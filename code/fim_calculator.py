import time
import sys
from typing import Dict
from argparse import Namespace

import torch
from torch import Tensor
from torch.distributions import Categorical
from torch.nn import Module
from torch.utils.data import DataLoader

from transformers import AutoModel, AutoTokenizer, AutoConfig, AutoModelForSequenceClassification
from datasets import load_dataset

import ipdb 

def fim_diag(model: Module,
             data_loader: DataLoader,
             samples_no: int = None,
             empirical: bool = False,
             device: torch.device = None,
             verbose: bool = False,
             every_n: int = None) -> Dict[int, Dict[str, Tensor]]:
    fim = {}
    for name, param in model.named_parameters():
        if param.requires_grad:
            fim[name] = torch.zeros_like(param)

    seen_no = 0
    last = 0
    tic = time.time()

    all_fims = dict({})

    while samples_no is None or seen_no < samples_no:
        data_iterator = iter(data_loader)
        try:
            # data, target = next(data_iterator)
            batch = next(data_iterator)
            data, target = batch["input_ids"], batch["label"]
        except StopIteration:
            if samples_no is None:
                break
            data_iterator = iter(data_loader)
            data, target = next(data_loader)

        if device is not None:
            data = data.to(device)
            if empirical:
                target = target.to(device)

        logits = model(data).logits
        
        if empirical:
            outdx = target.unsqueeze(1)
        else:
            outdx = Categorical(logits=logits).sample().unsqueeze(1).detach()
        samples = logits.gather(1, outdx)

        idx, batch_size = 0, data.size(0)
        while idx < batch_size and (samples_no is None or seen_no < samples_no):
            model.zero_grad()
            torch.autograd.backward(samples[idx], retain_graph=True)
            for name, param in model.named_parameters():
                if param.requires_grad:
                    fim[name] += (param.grad * param.grad)
                    fim[name].detach_()
            seen_no += 1
            idx += 1

            if verbose and seen_no % 100 == 0:
                toc = time.time()
                fps = float(seen_no - last) / (toc - tic)
                tic, last = toc, seen_no
                sys.stdout.write(f"\rSamples: {seen_no:5d}. Fps: {fps:2.4f} samples/s.")

            if every_n and seen_no % every_n == 0:
                all_fims[seen_no] = {n: f.clone().div_(seen_no).detach_()
                                     for (n, f) in fim.items()}

    if verbose:
        if seen_no > last:
            toc = time.time()
            fps = float(seen_no - last) / (toc - tic)
        sys.stdout.write(f"\rSamples: {seen_no:5d}. Fps: {fps:2.5f} samples/s.\n")

    for name, grad2 in fim.items():
        grad2 /= float(seen_no)

    all_fims[seen_no] = fim

    return all_fims


model_name = "bert-base-cased"
glue_task_name = "sst2"

tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForSequenceClassification.from_pretrained(model_name)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model.to(device)

dataset = load_dataset("glue", glue_task_name)["validation"]
# Randomly select 100 instances.
dataset = dataset.shuffle(seed=42).select(range(100))

batch_size = 8

def tokenize_function(example):
    return tokenizer(example["sentence"], truncation=True, padding=True, return_tensors="pt")

tokenized_data = dataset.map(tokenize_function, batched=True, remove_columns=["sentence", "idx"])
tokenized_data.set_format("torch", columns=["input_ids", "attention_mask", "label"])

data_loader = DataLoader(tokenized_data, batch_size=batch_size)

all_fims = fim_diag(
    model=model,
    data_loader=data_loader,
    samples_no=None,
    empirical=True,
    device=device,
    verbose=True,
    every_n=None
)

print(all_fims)

# Extract the latest FIM diagonal from the all_fims dictionary.
latest_fim_diag = all_fims[max(all_fims.keys())]

# Initialize a dictionary to store the FIM diagonal for each layer.
fim_diag_by_layer = {}

# Loop over the FIM diagonal for each parameter.
for param_name, param_fim_diag in latest_fim_diag.items():
    # Extract the layer name from the parameter name.
    layer_name = param_name.split('.')[0]

    # If the layer name is not in the fim_diag_by_layer dictionary, initialize it.
    if layer_name not in fim_diag_by_layer:
        fim_diag_by_layer[layer_name] = torch.zeros_like(param_fim_diag)
    
    # Accumulate the FIM diagonal for the layer.
    fim_diag_by_layer[layer_name] += param_fim_diag
    
print(fim_diag_by_layer)