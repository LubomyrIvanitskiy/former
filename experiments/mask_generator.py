import os

from _context import former
from former import util, GTransformer

from util import d, here

import torch
from torch import nn
from torch.autograd import Variable
import torch.nn.functional as F
import torch.distributions as dist

import numpy as np

from argparse import ArgumentParser
from torch.utils.tensorboard import SummaryWriter

import random, tqdm, sys, math, gzip

# NB, the enwik8 data contains tokens from 9 to 240, but well round up to the nearest
# power of two.
NUM_TOKENS = 128
# Used for converting between nats and bits
LOG2E = math.log2(math.e)

MODEL_PATH = "model_saved"


def sample(lnprobs, temperature=1.0):
    """
    Sample an element from a categorical distribution
    :param lnprobs: Outcome log-probabilities
    :param temperature: Sampling temperature. 1.0 follows the given distribution,
        0.0 returns the maximum probability element.
    :return: The index of the sampled element.
    """

    if temperature == 0.0:
        return lnprobs.argmax()

    p = F.softmax(lnprobs / temperature, dim=0)
    cd = dist.Categorical(p)

    return cd.sample()


uk_letters = "абвгґдеєжзиійїклмнопрстуфхцчшщьюяqwertyuiopasdfghj"
uk_letters += uk_letters.upper()
uk_letters += "',.!?/-:;\" _1234567890+-=$()"
uk_letters

char_to_id = {ch: i for i, ch in enumerate(uk_letters)}
id_to_char = {i: ch for i, ch in enumerate(uk_letters)}


def ukwiki(path, n_train=int(764032), n_valid=int(5e4), n_test=int(5e4)):
    """
    Load the enwik8 dataset from the Hutter challenge.

    Adapted from https://github.com/openai/blocksparse/blob/master/examples/transformer/enwik8.py
    :param path:
    :param n_train:
    :param n_valid:
    :param n_test:
    :return:
    """
    with gzip.open(path) if path.endswith('.gz') else open(path) as file:
        X = np.array([char_to_id[ch] for ch in file.read(n_train + n_valid + n_test) if ch in char_to_id])
        print("".join([str(id_to_char[X[i]]) for i in range(4000, 4600)]))
        trX, vaX, teX = np.split(X, [n_train, n_train + n_valid])
        return torch.from_numpy(trX), torch.from_numpy(vaX), torch.from_numpy(teX)


def go(arg):
    if arg.seed < 0:
        seed = random.randint(0, 1000000)
        print('random seed: ', seed)
    else:
        torch.manual_seed(arg.seed)

    tbw = SummaryWriter(log_dir=arg.tb_dir)  # Tensorboard logging

    # load the data (validation unless arg.final is true, then test)
    arg.data = here('../wiki_uk.txt') if arg.data is None else arg.data

    data_train, data_val, data_test = ukwiki(arg.data)
    data_train, data_test = (torch.cat([data_train, data_val], dim=0), data_test) \
        if arg.final else (data_train, data_val)

    # create the model
    model = GTransformer(emb=arg.embedding_size, heads=arg.num_heads, depth=arg.depth, seq_length=arg.context,
                         num_tokens=NUM_TOKENS, wide=arg.wide)
    if os.path.exists(MODEL_PATH):
        model.load_state_dict(torch.load(MODEL_PATH))
    if torch.cuda.is_available():
        model.cuda()

    opt = torch.optim.Adam(lr=arg.lr, params=model.parameters())
    # linear learning rate warmup
    sch = torch.optim.lr_scheduler.LambdaLR(opt, lambda i: min(i / (arg.lr_warmup / arg.batch_size), 1.0))

    # training loop
    # - note: we don't loop over the data, instead we sample a batch of random subsequences each time.
    for i in tqdm.trange(arg.num_batches):

        opt.zero_grad()

        # sample a batch of random subsequences
        starts = torch.randint(size=(arg.batch_size,), low=0, high=data_train.size(0) - arg.context - 1)
        if arg.masked:
            seqs_source = [data_train.detach().clone()[start:start + arg.context, ] for start in starts]
            seqs_target = [data_train.detach().clone()[start:start + arg.context] for start in starts]
            for ss, st in zip(seqs_source, seqs_target):
                mask_indexes = torch.randint(1, arg.context, (arg.error_count,))
                for ind in mask_indexes:
                    ss[ind] = torch.tensor(char_to_id['$'])
                # print(''.join([id_to_char[s.item()] for s in ss]))
                # print(''.join([id_to_char[t.item()] for t in st]))
        else:
            seqs_source = [data_train[start:start + arg.context] for start in starts]
            seqs_target = [data_train[start + 1:start + arg.context + 1] for start in starts]

        source = torch.cat([s[None, :] for s in seqs_source], dim=0).to(torch.long)
        target = torch.cat([s[None, :] for s in seqs_target], dim=0).to(torch.long)
        # - target is the same sequence as source, except one character ahead

        if torch.cuda.is_available():
            source, target = source.cuda(), target.cuda()
        source, target = Variable(source), Variable(target)

        output = model(source)

        loss = F.nll_loss(output.transpose(2, 1), target, reduction='mean')
        tbw.add_scalar('transformer/train-loss', float(loss.item()) * LOG2E, i * arg.batch_size)

        loss.backward()

        # clip gradients
        # - If the total gradient vector has a length > 1, we clip it back down to 1.
        if arg.gradient_clipping > 0.0:
            nn.utils.clip_grad_norm_(model.parameters(), arg.gradient_clipping)

        opt.step()
        sch.step()

        # - validate every {arg.test_every} steps. First we compute the
        #   compression on the validation (or a subset)
        #   then we generate some random text to monitor progress
        if i != 0 and (i % arg.test_every == 0 or i == arg.num_batches - 1):

            upto = data_test.size(0) if i == arg.num_batches - 1 else arg.test_subset
            data_sub = data_test[:upto]

            with torch.no_grad():
                bits, tot = 0.0, 0
                batch = []  # buffer, every time it fills up, we run it through the model

                # for current in range(data_sub.size(0)):

                #     fr = max(0, current - arg.context)
                #     to = current + 1

                #     context = data_sub[fr:to].to(torch.long)
                #     if context.size(0) < arg.context + 1:
                #         pad = torch.zeros(size=(arg.context + 1 - context.size(0),), dtype=torch.long)
                #         context = torch.cat([pad, context], dim=0)

                #         assert context.size(0) == arg.context + 1

                #     if torch.cuda.is_available():
                #         context = context.cuda()

                #     batch.append(context[None, :])

                #     if len(batch) == arg.test_batchsize or current == data_sub.size(0) - 1:

                #         # batch is full, run it through the model
                #         b = len(batch)

                #         all = torch.cat(batch, dim=0)
                #         source = all[:, :-1] # input
                #         target = all[:, -1]  # target values

                #         output = model(source)

                #         lnprobs = output[torch.arange(b, device=d()), -1, target]
                #         log2probs = lnprobs * LOG2E # convert from nats to bits

                #         bits += - log2probs.sum()
                #         batch = [] # empty buffer

                # bits_per_byte = bits / data_sub.size(0)

                # # print validation performance. 1 bit per byte is (currently) state of the art.
                # print(f'epoch{i}: {bits_per_byte:.4} bits per byte')
                # tbw.add_scalar(f'transformer/eval-loss', bits_per_byte, i * arg.batch_size)

                # generate some random text
                GENSIZE = 600
                TEMP = 0.5
                seedfr = random.randint(0, data_test.size(0) - arg.context)
                # input = data_test[seedfr:seedfr + arg.context].to(torch.long)
                test_msgs = [
                    "купила м$ма коника, а коник і шо",
                    "як тебе не лю$ити Києве мій коли",
                    "у л$сі лісі темному де ходить як"]
                for test_msg in test_msgs:
                    test_data = np.zeros(arg.context)
                    test_data.fill(110)
                    test_data[0:len(test_msg)] = np.array([char_to_id[ch] for ch in test_msg])
                    input = torch.from_numpy(test_data).to(torch.long)

                    if torch.cuda.is_available():
                        input = input.cuda()

                    input = Variable(input)

                    print('[', end='', flush=True)
                    for c in input:
                        print(str(id_to_char[c.item()]), end='', flush=True)
                    print(']', end='', flush=True)

                    output = model(input[None, :])
                    out_string = ''.join([id_to_char[ind.item()] for ind in output[0].max(axis=1).indices])
                    # c = sample(output[0].max(axis=1), TEMP)
                    print("Foo1")
                    print("PRED: " + out_string)

                    print("Foo2")
                    print()

        # Save model
        torch.save(model.state_dict(), MODEL_PATH)


if __name__ == "__main__":
    ## Parse the command line options
    parser = ArgumentParser()

    parser.add_argument("-N", "--num-batches",
                        dest="num_batches",
                        help="Number of batches to train on. Each batch contains randomly sampled subsequences of the data.",
                        default=20, type=int)

    parser.add_argument("-b", "--batch-size",
                        dest="batch_size",
                        help="The batch size.",
                        default=64, type=int)

    parser.add_argument("-D", "--data", dest="data",
                        help="Data file. Will be read as a string of 8-bit characters.",
                        default=None)

    parser.add_argument("-l", "--learn-rate",
                        dest="lr",
                        help="Learning rate",
                        default=0.0001, type=float)

    parser.add_argument("-T", "--tb_dir", dest="tb_dir",
                        help="Tensorboard logging directory",
                        default='./runs')

    parser.add_argument("-f", "--final", dest="final",
                        help="Whether to run on the real test set (if not included, the validation set is used).",
                        action="store_true")

    parser.add_argument("-E", "--embedding", dest="embedding_size",
                        help="Size of the character embeddings.",
                        default=512, type=int)

    parser.add_argument("-H", "--heads", dest="num_heads",
                        help="Number of attention heads.",
                        default=8, type=int)

    parser.add_argument("-C", "--context", dest="context",
                        help="Length of the sequences extracted from the corpus (and the context used during inference).",
                        default=32, type=int)

    parser.add_argument("-d", "--depth", dest="depth",
                        help="Depth of the network (nr of self-attention layers)",
                        default=12, type=int)

    parser.add_argument("-r", "--random-seed",
                        dest="seed",
                        help="RNG seed. Negative for random",
                        default=1, type=int)

    parser.add_argument("--test-every",
                        dest="test_every",
                        help="How many batches between tests.",
                        default=20, type=int)

    parser.add_argument("--test-subset",
                        dest="test_subset",
                        help="A subset for the validation tests.",
                        default=100000, type=int)

    parser.add_argument("--test-batchsize",
                        dest="test_batchsize",
                        help="Batch size for computing the validation loss. This can be a bit bigger than the training batch size.",
                        default=64, type=int)

    parser.add_argument("--gradient-clipping",
                        dest="gradient_clipping",
                        help="Gradient clipping.",
                        default=1.0, type=float)

    parser.add_argument("--lr-warmup",
                        dest="lr_warmup",
                        help="Learning rate warmup.",
                        default=5000, type=int)

    parser.add_argument("--wide", dest="wide",
                        help="Use wide self attention instead of narrow self attention.",
                        action="store_true",
                        default=False)

    parser.add_argument("--masked",
                        dest="masked",
                        help="Masked mode. Try to detected masked letter",
                        default=True)

    parser.add_argument("--error-count",
                        dest="error_count",
                        help="For masked. How many errors available",
                        default=1, type=int)

    options = parser.parse_args()

    print('OPTIONS ', options)

    go(options)
