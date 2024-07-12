from dataclasses import dataclass
import inspect
import math
import torch
import torch.nn as nn
from torch.nn import functional as F
import tiktoken
import sys
import time
import os
import numpy as np

#--------------------------------------------------------------------------------------

class CasualSelfAttention(nn.Module):

    def __init__(self, config) -> None:
        super().__init__()
        assert config.n_embd % config.n_head == 0
        #key, query, value projection for all heads, but in a batch
        self.c_attn = nn.Linear(config.n_embd, config.n_embd * 3)
        # output projection
        self.c_proj = nn.Linear(config.n_embd, config.n_embd)
        #regularization
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        # not really a 'bias', but more of a mask, but following the OPENAI/HF naming though
        self.register_buffer("bias", torch.tril(torch.ones(config.block_size, config.block_size))
                             .view(1, 1, config.block_size, config.block_size))
        
    def forward(self, x):
        B,T,C = x.size() # batch_size, sequnece length, embedding dimensionality (n_embd)
        # calculate query, key and values for all heads in batch and move head forward to be the batch dim
        # nh is "number of heads", hs is "head size", and C (number of channels) = nh * hs
        # e.g. in GPT-2 (124M), n_head=12, hs=64, so nh*hs=C=768 channels in the Transformer
        qkv = self.c_attn(x)
        q,k,v = qkv.split(self.n_embd, dim=2)
        k = k.view(B, T, self.n_head, C//self.n_head).transpose(1, 2)# (B, nh, T, hs)
        q = q.view(B, T, self.n_head, C//self.n_head).transpose(1, 2)# (B, nh, T, hs)
        v = v.view(B, T, self.n_head, C//self.n_head).transpose(1, 2)# (B, nh, T, hs)

        # attention (materializes the large (T,T) matrix for all the queries and keys)
        # att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        # att = att.masked_fill(self.bias[:, :, :T, :T] == 0, float('-inf'))
        # att = F.softmax(att, dim=-1)
        # y = att @ v # (B, nh, T, T) @ (B, nh, T, hs) -> (B, nh, T, hs) weighted sum of values

        # in order to speed up the process in GPU we use flash attention for the above caluculations
        y = F.scaled_dot_product_attention(q,k,v,is_causal=True)# this is the flash attention function
        y = y.transpose(1, 2).contiguous().view(B, T, C)# re-assemble all head outputs side by side
        #output projection
        y = self.c_proj(y)
        return y
    
# This is the GELU activation function with a tanh approximation
# class TanhGELU(nn.Module):
#     def forward(self, x):
#         return 0.5 * x * (1 + torch.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * torch.pow(x, 3))))
# THe torch.compile function basically speeds up the operations here. As we see in the Gelu function,
# the input x is used multiple times which is stored in the memory of the GPU. THe travel of this data
# from memory to GPU is very slow. So, torch.compile function speeds up the operations by storing the
# data in the GPU memory itself. This is done by the torch.compile function.

class MLP(nn.Module):

    def __init__(self, config) -> None:
        super().__init__()
        self.c_fc = nn.Linear(config.n_embd, 4*config.n_embd)
        self.gelu = nn.GELU(approximate="tanh")# GELU: Gaussian Error Linear Unit:- non-linear activation function which is similar to RELU but is not sharp at 0 
        #The approximate version was developed because the original was very slow in tensorflow
        # But now we don't need to use the approximate version because we are using PyTorch
        # But since we are replicating GPT-2, we will use the approximate version
        self.c_proj = nn.Linear(config.n_embd * 4, config.n_embd)
        self.c_proj.NANOGPT_SCALE_INIT = 1

    def forward(self, x):
        x = self.c_fc(x)
        x = self.gelu(x)
        x = self.c_proj(x)
        return x

class Block(nn.Module):

    def __init__(self, config) -> None:
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.n_embd)
        self.attn = CasualSelfAttention(config)# this is an aggregation function where tokens talk to each other or exchange information
        self.ln_2 = nn.LayerNorm(config.n_embd)
        self.mlp = MLP(config)# this is a feedforward neural network: happens to each token independently

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))# add skip connection
        x = x + self.mlp(self.ln_2(x))# 
        return x

@dataclass
class GPTConfig:
    block_size: int = 1024# max sequence length
    vocab_size: int = 50257 # number of tokens: 50000 BPE merges + 256 byte tokens + 1 <|endoftext|> token
    n_layer: int = 12# number of layers
    n_head: int = 12# number of heads
    n_embd: int = 768# embedding dimension

class GPT(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.config = config

        self.transformer = nn.ModuleDict(dict(
            wte = nn.Embedding(config.vocab_size, config.n_embd),# wte: token embeddings
            wpe = nn.Embedding(config.block_size, config.n_embd),#wpe = position encodings
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),# h: transformer blocks
            ln_f = nn.LayerNorm(config.n_embd),#ln_f: final layer normalization
        ))
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)

        # According to the GPT2 paper, the weights of the wte and the lm_head should be tied, i.e.
        # the weights of the embedding layer should be the same as the weights of the final linear layer
        # This is done to reduce the number of parameters in the model
        # We will implement this by sharing the weights of the embedding layer and the final linear layer
        self.transformer.wte.weight = self.lm_head.weight

        # weigth initialization
        self.apply(self._init_weights)# apply the _init_weights function to all the parameters of the model

    # This function initializes the weights of the model
    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            std = 0.02
            if hasattr(module, 'NANOGPT_SCALE_INIT'):
                std *= (2*self.config.n_layer)**-0.5# the scale of the initialization depends on the number of residual layers
                # The number of residual layers here is 2*self.config.n_layer because there are two residual layers in each block
            torch.nn.init.normal_(module.weight, mean=0.0, std=std)
            if module.bias is not None:# if the module has a bias then initialize it to zero
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, idx, targets=None):
        # idx is of shape (B,T) where B is batch size and T is the sequence length
        B,T = idx.size()
        assert T<= self.config.block_size, "Cannot forward sequence of length {T}, block size is {self.config.block_size}"
        # forward the token and position embeddings
        pos = torch.arange(T, dtype=torch.long, device=idx.device)# shape (T)
        pos_emb = self.transformer['wpe'](pos)# positional embeddings of shape (T, n_embd)
        tok_emb = self.transformer.wte(idx)# token embeddings of shape (B, T, n_embd)
        x = tok_emb + pos_emb
        # forward the blocks of transformer
        for block in self.transformer.h:
            x = block(x)
        # forward the final layer norm and the classifier
        x = self.transformer.ln_f(x)
        logits = self.lm_head(x)# (B,T,vocab_size)
        # Calculating the loss
        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))# The cross entropy does not take multidimensional inputs
            # We need to flatten the logits and targets
        return logits, loss

    @classmethod
    def from_pretrained(cls, model_type):
        """Loads pretrained GPT-2 model weights from huggingface"""
        assert model_type in {'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl'}
        from transformers import GPT2LMHeadModel
        print("loading weights from pretrained gpt: %s" % model_type)

        # n_layer, n_head and n_embd are determined from model_type
        config_args = {
            'gpt2':         dict(n_layer=12, n_head=12, n_embd=768),  # 124M params
            'gpt2-medium':  dict(n_layer=24, n_head=16, n_embd=1024), # 350M params
            'gpt2-large':   dict(n_layer=36, n_head=20, n_embd=1280), # 774M params
            'gpt2-xl':      dict(n_layer=48, n_head=25, n_embd=1600), # 1558M params
        }[model_type]
        config_args['vocab_size'] = 50257 # always 50257 for GPT model checkpoints
        config_args['block_size'] = 1024 # always 1024 for GPT model checkpoints
        # create a from-scratch initialized minGPT model
        config = GPTConfig(**config_args)
        model = GPT(config)
        sd = model.state_dict()
        sd_keys = sd.keys()
        sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')] # discard this mask / buffer, not a param

        # init a huggingface/transformers model
        model_hf = GPT2LMHeadModel.from_pretrained(model_type)
        sd_hf = model_hf.state_dict()

        # copy while ensuring all of the parameters are aligned and match in names and shapes
        sd_keys_hf = sd_hf.keys()
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias')] # ignore these, just a buffer
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.bias')] # same, just the mask (buffer)
        transposed = ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']
        # basically the openai checkpoints use a "Conv1D" module, but we only want to use a vanilla Linear
        # this means that we have to transpose these weights when we import them
        assert len(sd_keys_hf) == len(sd_keys), f"mismatched keys: {len(sd_keys_hf)} != {len(sd_keys)}"
        for k in sd_keys_hf:
            if any(k.endswith(w) for w in transposed):
                # special treatment for the Conv1D weights we need to transpose
                assert sd_hf[k].shape[::-1] == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k].t())
            else:
                # vanilla copy over the other parameters
                assert sd_hf[k].shape == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k])

        return model
    

    def configure_optimizers(self, weight_decay, learning_rate, device):
        # start with all of the candidate parameters (that require grad)
        param_dict = {pn: p for pn, p in self.named_parameters()}
        param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
        # create optim groups. Any parameters that is 2D will be weight decayed, otherwise no.
        # i.e. all weight tensors in matmuls + embeddings decay, all biases layernorms dont.
        decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
        nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]
        optim_groups = [
            {'params': decay_params, 'weight_decay': weight_decay},
            {'params': nodecay_params, 'weight_decay': 0.0}
        ]
        num_decay_params = sum(p.numel() for p in decay_params)
        num_nodecay_params = sum(p.numel() for p in nodecay_params)
        print(f"num decayed parameter tensors: {len(decay_params)}, with {num_decay_params:,} params")
        print(f"num non-decayed parameter tensors: {len(nodecay_params)}, with {num_nodecay_params:,} params")
        # Create AdamW optimizer and use the fused version if it is available
        fused_available = 'fused' in inspect.signature(torch.optim.AdamW).parameters
        use_fused = fused_available and 'cuda' in device# it basically fuses all the kernel operations into a single kernel
        # this is faster than the non-fused version
        print(f"using fused AdamW: {use_fused}")
        optimizer = torch.optim.AdamW(optim_groups, lr=learning_rate, betas=(0.9, 0.95), eps=1e-8, fused=use_fused)
        return optimizer

# --------------------------------------------------------------------------------------

def load_tokens(filename):
    npt = np.load(filename)
    npt = npt.astype(np.int32) # added after video
    ptt = torch.tensor(npt, dtype=torch.long)
    return ptt


# We create a Dataloader class to feed the data in batches to the model
class DataloaderLite:
    def __init__(self,B,T, process_rank, num_processes, split):
        self.B = B
        self.T = T

        # # at init load tokens from disk and store them in memory
        # with open("input.txt", "r") as f:
        #     text = f.read()
        # enc = tiktoken.get_encoding('gpt2')
        # tokens = enc.encode(text)
        # self.tokens = torch.tensor(tokens)# (N,)
        # print(f"total number of tokens: {len(self.tokens)}")
        # print(f"1 epoch = {len(self.tokens)//(B*T)} batches")# number of batches in one epoch

        # #state
        # self.current_position = 0

        # Now we will load the tokens from the numpy file
        self.process_rank = process_rank
        self.num_processes = num_processes
        assert split in {'train','val'}

        # get the shard filenames
        data_root = "edu_fineweb10B"
        shards = os.listdir(data_root)
        shards = [s for s in shards if split in s]
        shards = sorted(shards)
        shards = [os.path.join(data_root, s) for s in shards]
        self.shards = shards
        assert len(shards)>0, f"no shards found for split {split}"
        if master_process:
            print(f"found {len(shards)} shards for split {split}")
        self.reset()

    def reset(self):
        # state, init at shard zero
        self.current_shard = 0
        self.tokens = load_tokens(self.shards[self.current_shard])
        self.current_position = self.B * self.T * self.process_rank

    def next_batch(self):
        B,T = self.B, self.T
        # get the next batch
        buf = self.tokens[self.current_position: self.current_position+B*T+1]# We add an additional token so that we can have a target sequence
        x = buf[:-1].view(B,T)# (B,T): inputs
        y = buf[1:].view(B,T)# (B,T): targets
        # advance the positions in the tensor
        self.current_position += B*T * self.num_processes
        # if loading the next batch would overrun the tokens, reset the position
        if self.current_position + (B*T*self.num_processes + 1) > len(self.tokens):
            self.current_shard = (self.current_shard + 1) % len(self.shards)
            self.tokens = load_tokens(self.shards[self.current_shard])
            self.current_position = self.B * self.T * self.process_rank
        return x,y



# --------------------------------------------------------------------------------------
# Running the training loop
from torch.distributed import init_process_group, destroy_process_group
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist

#setup DDP( Distributed Data Parallel). This is used to train the model on multiple GPUs
# torchrun command sets the env variable RANK, LOCAL_RANK, and WORLD_SIZE
ddp = int(os.environ.get('RANK',-1)) != -1 # is this a DDP process?
if ddp:
    #use of ddp atm demands CUDA, we set the device appropriately according to rank
    assert torch.cuda.is_available(), "only CUDA is supported for DDP"
    init_process_group(backend='nccl')
    ddp_rank = int(os.environ['RANK'])
    ddp_local_rank = int(os.environ['LOCAL_RANK'])
    ddp_world_size = int(os.environ['WORLD_SIZE'])
    device = f'cuda:{ddp_local_rank}'
    torch.cuda.set_device(device)
    master_process = ddp_rank == 0 # this process will do logging, checkpointing etc.
else:
    # vanilla, non-DDP run
    ddp_rank = 0
    ddp_local_rank = 0
    ddp_world_size = 1
    master_process = True
    # attempt to autodetect the device
    device = "cpu"
    if torch.cuda.is_available():
        device = "cuda"
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        device = "mps"
    print("using device: ", device)

torch.manual_seed(1337)
if torch.cuda.is_available():
    torch.cuda.manual_seed(1337)


# Now according to the GPT3 paper they used 0.5M token batch size for training:
total_batch_size = 524288 # 2**19, ~0.5M tokens
B = 8 # batch size
T = 1024 # sequence length
assert total_batch_size % (B*T*ddp_world_size) == 0, "make sure the total batch size is divisible by B * T"
# Now we use gradient accumulation to simulate a larger batch size
grad_accum_Steps = total_batch_size // (B*T*ddp_world_size)
if master_process:
    print(f"total batch size: {total_batch_size}")
    print(f"gradient accumulation steps: {grad_accum_Steps}")



# enc = tiktoken.get_encoding('gpt2')
# with open("input.txt", "r") as f:
#     text = f.read()
# text = text[:1000]
# tokens = enc.encode(text)
# B,T = 4,32
# buf = torch.tensor(tokens[:B*T + 1])# We add an additional token so that we can have a target sequence
# buf = buf.to(device)
# x = buf[:-1].view(B,T)# (B,T)
# y = buf[1:].view(B,T)# (B,T)

# The above code overfits the data because it uses the same data for training and validation
# But now we will use a Dataloader class to feed the data in batches to the model, hence 
# #we expect the model to not overfit a single batch 

# We use a Dataloader now instead of the above code
# train_loader = DataloaderLite(4,32)
train_loader = DataloaderLite(B=B, T=T, process_rank=ddp_rank, num_processes=ddp_world_size, split='train')
val_loader = DataloaderLite(B=B, T=T, process_rank=ddp_rank, num_processes=ddp_world_size, split='val')

torch.set_float32_matmul_precision("high")# high precision for matrix multiplication: this gives better throughput on the GPU

model = GPT(GPTConfig(vocab_size=50304))# increasing the number of fake tokens, so that the number of tokens is power of 2
model.eval()
model.to(device)
model = torch.compile(model)# compile the model to TorchScript for better performance
if ddp:
    model = DDP(model, device_ids=[ddp_local_rank])
raw_model = model.module if ddp else model # if DDP is used, then model is a wrapper around the actual model

# Implementing Learning Rate Scheduler
max_lr = 6e-4# According to GPT3 paper for the GPT3 small model
min_lr = max_lr * 0.1# According to the GPT3 paper
warmup_steps = 715 #10
max_steps = 19073#50
def get_lr(it):
    # 1) linear warmup for warmup_iters steps
    if it < warmup_steps:
        return max_lr * (it+1) / warmup_steps
    # 2) if it > lr_decay_iters, return min learning rate
    if it >= max_steps:
        return min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_steps) / (max_steps - warmup_steps)
    assert 0<= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))# coeff starts at 1 and goes to 0
    return min_lr + coeff * (max_lr - min_lr)


# Optimization
# optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4, betas=(0.9, 0.95), eps=1e-8)# AdamW optimizer
optimizer = raw_model.configure_optimizers(weight_decay=0.1, learning_rate=6e-4, device=device)# This is the optimizer used in the GPT3 paper

for step in range(max_steps):
    t0 = time.time()

    # once in a while evaluate our validation loss
    if step % 100 == 0:
        model.eval()
        val_loader.reset()
        with torch.no_grad():
            val_loss_accum = 0.0
            val_loss_steps = 20
            for _ in range(val_loss_steps):
                x,y = val_loader.next_batch()
                x,y = x.to(device), y.to(device)
                logits, loss = model(x,y)
                loss = loss / val_loss_steps
                val_loss_accum += loss.detach()
        if ddp:
            dist.all_reduce(val_loss_accum, op=dist.ReduceOp.AVG)
        if master_process:
            print(f"validation loss: {val_loss_accum.item():.4f}")


    # once in a while generate from the model (except step 0, which is noise)
    if step >0 and step % 100 == 0:
        model.eval()
        num_return_sequences = 4
        max_length = 32
        # prefix tokens
        enc = tiktoken.get_encoding('gpt2')
        tokens = enc.encode("Hi, I'm a language model,")
        tokens = torch.tensor(tokens,dtype=torch.long)# (8,)
        tokens = tokens.unsqueeze(0).repeat(num_return_sequences, 1)# (5,8)
        xgen = tokens.to(device)
        # generate right now x is (B,T) where B is 5 and T is 8
        sample_rng = torch.Generator(device=device)
        sample_rng.manual_seed(42+dpp_rank)# set the seed for the random number generator
        while xgen.size(1) < max_length:
            # forward the model to get the logits
            with torch.no_grad():
                logits,loss = model(xgen)# (B,T, vocab_size)
                # take the logits at the last position
                logits = logits[:, -1, :]# (B, vocab_size)
                # get the probabilities
                probs = F.softmax(logits, dim=-1)
                # do top-k sampling of 50 (huggingface pipeline default)
                # topk_probs here becomes (5,50), topk_indices is (5,50)
                topk_probs, topk_indices = torch.topk(probs, 50, dim=-1)
                # select a token from the top-k probabilities
                # note: mutinomial does not demand the input  to sum to 1
                ix = torch.multinomial(topk_probs, num_samples=1, generator=sample_rng)# (B,1)
                # gather the corresponding indices
                xcol = torch.gather(topk_indices, -1, ix)# (B,1)
                # append to the sequence
                xgen = torch.cat((xgen, xcol), dim=1)
        # print yhe generated text
        for i in range(num_return_sequences):
            generated = xgen[i, :max_length].tolist()
            text = enc.decode(generated)
            print(">", text)


    # Training loop
    model.train()
    optimizer.zero_grad()
    loss_accum = 0.0
    for micro_steps in range(grad_accum_Steps):
        x,y = train_loader.next_batch()
        x,y = x.to(device), y.to(device)
        # with torch.autocast(device_type=device, dtype=torch.bfloat16):# use bfloat16 for training as it is faster
        #     logits, loss = model(x,y)
        # The above code is not working because the my current GPU is not supporting bfloat16
        logits, loss = model(x,y)
        loss = loss / grad_accum_Steps# scale the loss
        loss_accum += loss.detach()# accumulate the loss for all the gradient accumulation steps
        if ddp:
            model.require_backward_grad_sync = (micro_steps == grad_accum_Steps - 1)# sync the gradients across all the GPUs
        loss.backward()# deposists the gradients in the parameters
    if ddp:
        dist.all_reduce(loss_accum, op=dist.ReduceOp.AVG)# average the loss across all the GPUs
    norm = nn.utils.clip_grad_norm_(model.parameters(), 1.0)# clip the gradients to avoid exploding gradients
    # this is basically performed if suppose we get a bad batch which results in a very high loss which could then lead to a high gradient
    # and this could shock the model and could lead to a bad model
    lr = get_lr(step)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    optimizer.step()# updates the parameters
    torch.cuda.synchronize()# wait for the GPU to finish the current iteration
    t1 = time.time()
    dt = (t1 - t0)*1000 # time difference in milliseconds
    tokens_processed = train_loader.B * train_loader.T * grad_accum_Steps * ddp_world_size
    tokens_per_sec = tokens_processed / dt
    if master_process:
        print(f"step {step:4d} | loss: {loss_accum.item()} | lr: {lr:.4e} | norm: {norm:.4f} | dt: {dt:.2f}ms | tokens/sec: {tokens_per_sec:.2f}")


if ddp:
    destroy_process_group()

sys.exit(0)

# num_return_sequences = 5
# max_length = 50
# # prefix tokens
# enc = tiktoken.get_encoding('gpt2')
# tokens = enc.encode("Hi, I'm a language model,")
# tokens = torch.tensor(tokens,dtype=torch.long)# (8,)
# tokens = tokens.unsqueeze(0).repeat(num_return_sequences, 1)# (5,8)
# x = tokens.to(device)


# # generate right now x is (B,T) where B is 5 and T is 8
# # set the seed to 42
# torch.manual_seed(42)
# torch.cuda.manual_seed(42)
# while x.size(1) < max_length:
#     # forward the model to get the logits
#     with torch.no_grad():
#         logits = model(x)# (B,T, vocab_size)
#         # take the logits at the last position
#         logits = logits[:, -1, :]# (B, vocab_size)
#         # get the probabilities
#         probs = F.softmax(logits, dim=-1)
#         # do top-k sampling of 50 (huggingface pipeline default)
#         # topk_probs here becomes (5,50), topk_indices is (5,50)
#         topk_probs, topk_indices = torch.topk(probs, 50, dim=-1)
#         # select a token from the top-k probabilities
#         ix = torch.multinomial(topk_probs, num_samples=1)# (B,1)
#         # gather the corresponding indices
#         xcol = torch.gather(topk_indices, -1, ix)# (B,1)
#         # append to the sequence
#         x = torch.cat((x, xcol), dim=1)


# # print yhe generated text
# for i in range(num_return_sequences):
#     generated = x[i, :max_length].tolist()
#     text = enc.decode(generated)
#     print(">", text)

# Moved the code up between eval and training loop to generate the text