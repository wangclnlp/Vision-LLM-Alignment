# DeepSpeed Team
import math
import torch
import os
import deepspeed
import sys
from transformers import AdamW
from transformers import get_scheduler
sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir)))
from utils.model import create_dsvl_model_and_transforms, create_reward_or_critic_model
from utils.ds_utils import get_train_ds_config
from utils.module.lora import convert_linear_layer_to_lora, only_optimize_lora_parameters
from utils.utils import get_optimizer_grouped_parameters, print_rank_0


class DeepSpeedRLHFEngine():
    def __init__(self, actor_model_name_or_path, 
                 reward_model_name_or_path,
                 actor_tokenizer=None,
                 reward_tokenizer=None,
                 number_dataset=None,
                 args=None):
        self.args = args

        self.number_dataset = number_dataset

        self.actor_tokenizer = actor_tokenizer
        self.reward_tokenizer = reward_tokenizer

        self.actor, self.actor_image_processor, self.actor_tokenizer_new = self._init_actor(
            actor_model_name_or_path)
        
        self.ref, self.ref_image_processor, self.ref_tokenizer_new = self._init_ref(
            actor_model_name_or_path)
        
        self.reward, self.reward_image_processor, self.reward_tokenizer_new = self._init_reward(
            reward_model_name_or_path)
        self.reward_tokenizer_new.padding_side="right"
        self.reward_tokenizer_new.add_bos_token = True
        self.reward_tokenizer_new.add_eos_token = True
        
        self.critic, self.critic_image_processor, self.critic_tokenizer_new = self._init_critic(
            reward_model_name_or_path)
        self.critic_tokenizer_new.padding_side="right"
        self.critic_tokenizer_new.add_bos_token = True
        self.critic_tokenizer_new.add_eos_token = True
        
    
    def _init_actor(self, actor_path):
        # DS Config
        ds_config = get_train_ds_config(
            offload=self.args.offload_actor_model,
            args=self.args,
            stage=2)
        ds_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_train_batch_size
        ds_config[
            'train_batch_size'] = self.args.per_device_train_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps
        ds_config['deepspeed_multinode_launcher'] = 'standard' 
        ds_config['gradient_accumulation_steps'] = self.args.gradient_accumulation_steps

        model, image_processor, tokenizer = create_dsvl_model_and_transforms(
                                            text_tokenizer=self.actor_tokenizer,
                                            args=self.args,
                                            ds_config=ds_config)
        
        print_rank_0("load actor model............")
        
        model.load_state_dict(torch.load(os.path.join(actor_path, 'pytorch_model.bin'), map_location='cpu'), strict=False) # Z3 wouldn't save pos embeddings (vis and rope)

        if self.args.lang_lora_dim > 0:
            model.lang_decoder = convert_linear_layer_to_lora(model.lang_decoder, self.args.lang_lora_module_name, self.args.lang_lora_dim)
        if self.args.only_optimize_lora:
            model.lang_decoder = only_optimize_lora_parameters(model.lang_decoder)

        if self.args.vis_lora_dim > 0:
            model.vis_encoder = convert_linear_layer_to_lora(model.vis_encoder, self.args.vis_lora_module_name, self.args.vis_lora_dim)
        if self.args.only_optimize_lora:
            model.vis_encoder = only_optimize_lora_parameters(model.vis_encoder)

        # Split weights in two groups, one with weight decay and the other not.
        optimizer_grouped_parameters = get_optimizer_grouped_parameters(
            model, self.args.weight_decay, small_lr=self.args.learning_rate_pretraining_components)

        optimizer = AdamW(optimizer_grouped_parameters,
                                lr=self.args.actor_learning_rate,
                                betas=(0.9, 0.95))

        num_update_steps_per_epoch = math.ceil(
            self.number_dataset / self.args.gradient_accumulation_steps)
        
        if self.args.num_warmup_steps <= 1:
            self.args.num_warmup_steps = int(self.args.num_warmup_steps * self.args.num_train_epochs * num_update_steps_per_epoch)
        else:
            self.args.num_warmup_steps = int(self.args.num_warmup_steps)

        lr_scheduler = get_scheduler(
            name=self.args.lr_scheduler_type,
            optimizer=optimizer,
            num_warmup_steps=self.args.num_warmup_steps,
            num_training_steps=self.args.num_train_epochs * num_update_steps_per_epoch,
        )

        ds_config = get_train_ds_config(
            offload=self.args.offload_actor_model,
            args=self.args,
            stage=self.args.actor_zero_stage)

        actor_engine, *_ = deepspeed.initialize(
                                            model=model,
                                            optimizer=optimizer,
                                            args=self.args,
                                            config=ds_config,
                                            lr_scheduler=lr_scheduler,
                                            dist_init_required=True)
        
        return actor_engine, image_processor, tokenizer

    def _init_ref(self, ref_path):
        # DS Config
        ds_config = get_train_ds_config(
            offload=self.args.offload_actor_model,
            args=self.args,
            stage=2)

        model, image_processor, tokenizer = create_dsvl_model_and_transforms(
                                            text_tokenizer=self.actor_tokenizer,
                                            args=self.args,
                                            ds_config=ds_config)
        
        print_rank_0("load ref model............")
        model.load_state_dict(torch.load(os.path.join(ref_path, 'pytorch_model.bin'), map_location='cpu'), strict=False) # Z3 wouldn't save pos embeddings (vis and rope)

        if self.args.lang_lora_dim > 0:
            model.lang_decoder = convert_linear_layer_to_lora(model.lang_decoder, self.args.lang_lora_module_name, self.args.lang_lora_dim)
        if self.args.only_optimize_lora:
            model.lang_decoder = only_optimize_lora_parameters(model.lang_decoder)

        if self.args.vis_lora_dim > 0:
            model.vis_encoder = convert_linear_layer_to_lora(model.vis_encoder, self.args.vis_lora_module_name, self.args.vis_lora_dim)
        if self.args.only_optimize_lora:
            model.vis_encoder = only_optimize_lora_parameters(model.vis_encoder)

        ds_config = get_train_ds_config(
            offload=self.args.offload_actor_model,
            args=self.args,
            stage=3)
        ref_engine, *_ = deepspeed.initialize(
                                        model=model,
                                        config=ds_config)
        
        return ref_engine, image_processor, tokenizer
    
    def _init_critic(self, critic_path):
        # DS Config
        ds_config = get_train_ds_config(
            offload=self.args.offload_critic_model,
            args=self.args,
            stage=2)
        ds_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_train_batch_size
        ds_config[
            'train_batch_size'] = self.args.per_device_train_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps
        ds_config['deepspeed_multinode_launcher'] = 'standard' 
        ds_config['gradient_accumulation_steps'] = self.args.gradient_accumulation_steps

        model, image_processor, tokenizer = create_reward_or_critic_model(
                                            text_tokenizer=self.reward_tokenizer,
                                            ds_config=ds_config,
                                            is_reward=True,
                                            is_load_from_ckpt=False,
                                            args=self.args)
                                    
        print_rank_0("load critic model............")
        model.load_state_dict(torch.load(os.path.join(critic_path, 'pytorch_model.bin'), map_location='cpu'), strict=False) # Z3 wouldn't save pos embeddings (vis and rope)

        if self.args.lang_lora_dim > 0:
            model.lang_decoder = convert_linear_layer_to_lora(model.lang_decoder, self.args.lang_lora_module_name, self.args.lang_lora_dim)
        if self.args.only_optimize_lora:
            model.lang_decoder = only_optimize_lora_parameters(model.lang_decoder)

        if self.args.vis_lora_dim > 0:
            model.vis_encoder = convert_linear_layer_to_lora(model.vis_encoder, self.args.vis_lora_module_name, self.args.vis_lora_dim)
        if self.args.only_optimize_lora:
            model.vis_encoder = only_optimize_lora_parameters(model.vis_encoder)

        # Split weights in two groups, one with weight decay and the other not.
        optimizer_grouped_parameters = get_optimizer_grouped_parameters(
            model, self.args.weight_decay, small_lr=self.args.learning_rate_pretraining_components)

        optimizer = AdamW(optimizer_grouped_parameters,
                                lr=self.args.critic_learning_rate,
                                betas=(0.9, 0.95))

        num_update_steps_per_epoch = math.ceil(
            self.number_dataset / self.args.gradient_accumulation_steps)
        
        if self.args.num_warmup_steps <= 1:
            self.args.num_warmup_steps = int(self.args.num_warmup_steps * self.args.num_train_epochs * num_update_steps_per_epoch)
        else:
            self.args.num_warmup_steps = int(self.args.num_warmup_steps)

        lr_scheduler = get_scheduler(
            name=self.args.lr_scheduler_type,
            optimizer=optimizer,
            num_warmup_steps=self.args.num_warmup_steps,
            num_training_steps=self.args.num_train_epochs * num_update_steps_per_epoch,
        )

        ds_config = get_train_ds_config(
            offload=self.args.offload_critic_model,
            args=self.args,
            stage=self.args.critic_zero_stage)

        critic_engine, *_ = deepspeed.initialize(
                                            model=model,
                                            optimizer=optimizer,
                                            args=self.args,
                                            config=ds_config,
                                            lr_scheduler=lr_scheduler,
                                            dist_init_required=True)
        
        return critic_engine, image_processor, tokenizer
    
    def _init_reward(self, reward_path):
        # DS Config
        ds_config = get_train_ds_config(
            offload=self.args.offload_critic_model,
            args=self.args,
            stage=2)
        ds_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_train_batch_size
        ds_config[
            'train_batch_size'] = self.args.per_device_train_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps
        ds_config['deepspeed_multinode_launcher'] = 'standard' 
        ds_config['gradient_accumulation_steps'] = self.args.gradient_accumulation_steps

        model, image_processor, tokenizer = create_reward_or_critic_model(
                                            text_tokenizer=self.reward_tokenizer,
                                            ds_config=ds_config,
                                            is_reward=True,
                                            is_load_from_ckpt=False,
                                            args=self.args)
                                    
        print_rank_0("load reward model............")
        model.load_state_dict(torch.load(os.path.join(reward_path, 'pytorch_model.bin'), map_location='cpu'), strict=False) # Z3 wouldn't save pos embeddings (vis and rope)

        if self.args.lang_lora_dim > 0:
            model.lang_decoder = convert_linear_layer_to_lora(model.lang_decoder, self.args.lang_lora_module_name, self.args.lang_lora_dim)
        if self.args.only_optimize_lora:
            model.lang_decoder = only_optimize_lora_parameters(model.lang_decoder)

        if self.args.vis_lora_dim > 0:
            model.vis_encoder = convert_linear_layer_to_lora(model.vis_encoder, self.args.vis_lora_module_name, self.args.vis_lora_dim)
        if self.args.only_optimize_lora:
            model.vis_encoder = only_optimize_lora_parameters(model.vis_encoder)

        ds_config = get_train_ds_config(
            offload=self.args.offload_critic_model,
            args=self.args,
            stage=3)
        reward_engine, *_ = deepspeed.initialize(
                                            model=model,
                                            config=ds_config)
        
        return reward_engine, image_processor, tokenizer