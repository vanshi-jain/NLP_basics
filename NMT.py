import io
from io import open
import os
import random
from typing import Tuple
from collections import Counter
import argparse
import time
import math
import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F
from torch import Tensor
import torchtext
from torchtext.vocab import vocab
from torchtext.data.utils import get_tokenizer
from collections import Counter
from nltk.translate.bleu_score import SmoothingFunction, sentence_bleu
import numpy as np
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader

def loadVoc(voc, tokenizer):
    counter= Counter()
    with open(voc, encoding="utf-8") as f:
        for s in f:
            counter.update(tokenizer(s))
    return vocab(counter, specials= ['<unk>', '<pad>', '<bos>', '<eos>'])

def dataPrep(en, de):
    print("loading data from " + en)
    print("loading data from " + de)

    raw_de_iter = iter(io.open(de, encoding="utf-8"))
    raw_en_iter = iter(io.open(en, encoding="utf-8"))
    data =[]
    for (raw_de, raw_en) in zip(raw_de_iter, raw_en_iter):
        de_tensor = torch.tensor([de_vocab[token] for token in de_token(raw_de)], \
                    dtype=torch.long)
        en_tensor = torch.tensor([en_vocab[token] for token in en_token(raw_en)],\
                    dtype=torch.long)
        data.append((de_tensor, en_tensor))
    return data

class Encoder(nn.Module):
    def __init__(self, input_dim: int,
                 emb_dim: int,
                 enc_hid_dim: int,
                 dec_hid_dim: int,
                 dropout: float):

        super(Encoder, self).__init__()

        self.input_dim = input_dim
        self.emb_dim = emb_dim
        self.enc_hid_dim = enc_hid_dim
        self.dec_hid_dim = dec_hid_dim
        self.dropout = dropout
        self.embedding = nn.Embedding(input_dim, emb_dim)
        self.rnn = nn.GRU(emb_dim, enc_hid_dim, bidirectional = True)
        self.fc = nn.Linear(enc_hid_dim * 2, dec_hid_dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, src: Tensor) -> Tuple[Tensor]:

        embedded = self.dropout(self.embedding(src))
        outputs, hidden = self.rnn(embedded)
        hidden = torch.tanh(self.fc(torch.cat((hidden[-2,:,:], \
                            hidden[-1,:,:]), dim = 1)))
        return outputs, hidden

class Decoder(nn.Module):
    def __init__(self, output_dim: int,
                 emb_dim: int,
                 enc_hid_dim: int,
                 dec_hid_dim: int,
                 dropout: int,
                 attention: nn.Module):

        super(Decoder, self).__init__()

        self.emb_dim = emb_dim
        self.enc_hid_dim = enc_hid_dim
        self.dec_hid_dim = dec_hid_dim
        self.output_dim = output_dim
        self.dropout = dropout
        self.attention = attention
        self.embedding = nn.Embedding(output_dim, emb_dim)
        self.rnn = nn.GRU((enc_hid_dim * 2) + emb_dim, dec_hid_dim)
        self.out = nn.Linear(self.attention.attn_in + emb_dim, output_dim)
        self.dropout = nn.Dropout(dropout)

    def _weighted_encoder_rep(self,
                              decoder_hidden: Tensor,
                              encoder_outputs: Tensor) -> Tensor:

        a = self.attention(decoder_hidden, encoder_outputs)
        a = a.unsqueeze(1)
        encoder_outputs = encoder_outputs.permute(1, 0, 2)
        weighted_encoder_rep = torch.bmm(a, encoder_outputs)
        weighted_encoder_rep = weighted_encoder_rep.permute(1, 0, 2)
        return weighted_encoder_rep

    def forward(self, input: Tensor,
                decoder_hidden: Tensor,
                encoder_outputs: Tensor) -> Tuple[Tensor]:

        input = input.unsqueeze(0)
        embedded = self.dropout(self.embedding(input))
        weighted_encoder_rep = self._weighted_encoder_rep(decoder_hidden,
                                                          encoder_outputs)
        rnn_input = torch.cat((embedded, weighted_encoder_rep), dim = 2)
        output, decoder_hidden = self.rnn(rnn_input, decoder_hidden.unsqueeze(0))
        embedded = embedded.squeeze(0)
        output = output.squeeze(0)
        weighted_encoder_rep = weighted_encoder_rep.squeeze(0)

        output = self.out(torch.cat((output,
                                     weighted_encoder_rep,
                                     embedded), dim = 1))

        return output, decoder_hidden.squeeze(0)
    
class Attention(nn.Module):
    def __init__(self,  enc_hid_dim: int,
                 dec_hid_dim: int,
                 attn_dim: int):

        super(Attention, self).__init__()

        self.enc_hid_dim = enc_hid_dim
        self.dec_hid_dim = dec_hid_dim
        self.attn_in = (enc_hid_dim * 2) + dec_hid_dim
        self.attn = nn.Linear(self.attn_in, attn_dim)

    def forward(self, decoder_hidden: Tensor,
                encoder_outputs: Tensor) -> Tensor:

        src_len = encoder_outputs.shape[0]
        repeated_decoder_hidden = decoder_hidden.unsqueeze(1).repeat(1, src_len, 1)
        encoder_outputs = encoder_outputs.permute(1, 0, 2)
        energy = torch.tanh(self.attn(torch.cat((
                            repeated_decoder_hidden,
                            encoder_outputs),
                            dim = 2)))
        attention = torch.sum(energy, dim=2)

        return F.softmax(attention, dim=1)

class Seq2Seq(nn.Module):
    def __init__(self,
                 encoder: nn.Module,
                 decoder: nn.Module,
                 device: torch.device):
        super().__init__()

        self.encoder = encoder
        self.decoder = decoder
        self.device = device

    def forward(self,
                src: Tensor,
                trg: Tensor,
                teacher_forcing_ratio: float = 0.5) -> Tensor:

        batch_size = src.shape[1]
        max_len = trg.shape[0]
        trg_vocab_size = self.decoder.output_dim

        outputs = torch.zeros(max_len, batch_size, trg_vocab_size).to(self.device)

        encoder_outputs, hidden = self.encoder(src)

        # first input to the decoder is the <sos> token
        output = trg[0,:]

        for t in range(1, max_len):
            output, hidden = self.decoder(output, hidden, encoder_outputs)
            outputs[t] = output
            teacher_force = random.random() < teacher_forcing_ratio
            top1 = output.max(1)[1]
            output = (trg[t] if teacher_force else top1)

        return outputs

def train(model: nn.Module,
          iterator: torch.utils.data.DataLoader,
          optimizer: optim.Optimizer,
          criterion: nn.Module,
          clip: float):

    model.train()
    epoch_loss = 0
    
    for _, (src, trg) in enumerate(iterator):
        src, trg = src.to(device), trg.to(device)

        optimizer.zero_grad()

        output = model(src, trg)

        output = output[1:].view(-1, output.shape[-1])
        trg = trg[1:].view(-1)

        loss = criterion(output, trg)

        loss.backward()

        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)

        optimizer.step()

        epoch_loss += loss.item()

    return epoch_loss / len(iterator)

def evaluate(model: nn.Module,
             iterator: torch.utils.data.DataLoader,
             criterion: nn.Module):

    model.eval()
    epoch_loss = 0

    with torch.no_grad():
        for _, (src, trg) in enumerate(iterator):
            src, trg = src.to(device), trg.to(device)

            output = model(src, trg, 0) #turn off teacher forcing

            output = output[1:].view(-1, output.shape[-1])
            trg = trg[1:].view(-1)

            loss = criterion(output, trg)

            epoch_loss += loss.item()

    return epoch_loss / len(iterator)

def epoch_time(start_time: int,
               end_time: int):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))

    return elapsed_mins, elapsed_secs

def testBleuScore(encoder, decoder, pairs):
    smooth= SmoothingFunction().method1
    bleu= []
    for pair in pairs:
        try:
            output_words, attentions= evaluate(encoder, decoder, pair[0])
        except RuntimeError:
            pass
        output_sent = ''.join(output_words)
        bleu.append(sentence_bleu([output_sent], pair[1], smooth))
    bleu_mean= np.mean(bleu)
    return bleu_mean

def generate_batch(data_batch):
  de_batch, en_batch = [], []
  for (de_item, en_item) in data_batch:
    de_batch.append(torch.cat([torch.tensor([BOS_IDX]), de_item, torch.tensor([EOS_IDX])], dim=0))
    en_batch.append(torch.cat([torch.tensor([BOS_IDX]), en_item, torch.tensor([EOS_IDX])], dim=0))
  de_batch = pad_sequence(de_batch, padding_value=PAD_IDX)
  en_batch = pad_sequence(en_batch, padding_value=PAD_IDX)

  return de_batch, en_batch

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Neural Machine Translation using GRU")
    parser.add_argument('mode', type=str, help='Mode: train/ test/ translate')
    arg= parser.parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    BATCH_SIZE =128
    
    MAX_LEN= 50
    teacher_forcing_ratio = 0.8

    train_path = ['/home/vanshika/Desktop/NLP_basics/dataset/train/traindatade.txt',
                  '/home/vanshika/Desktop/NLP_basics/dataset/train/traindataen.txt']
    test_path = ['/home/vanshika/Desktop/NLP_basics/dataset/test/testdatade.txt',
                 '/home/vanshika/Desktop/NLP_basics/dataset/test/testdataen.txt']
    vocab_path= ['/home/vanshika/Desktop/NLP_basics/dataset/vocab/vocde.txt',
                 '/home/vanshika/Desktop/NLP_basics/dataset/vocab/vocen.txt']


    de_token = get_tokenizer('spacy', language='de')
    en_token = get_tokenizer('spacy', language='en')

    de_vocab = loadVoc(train_path[0], de_token)
    en_vocab = loadVoc(train_path[1], en_token)    

    de_vocab.set_default_index(de_vocab['<unk>'])
    en_vocab.set_default_index(en_vocab['<unk>'])

    PAD_IDX = de_vocab['<pad>']
    BOS_IDX = de_vocab['<bos>']
    EOS_IDX = de_vocab['<eos>']

    train_data = dataPrep(train_path[1], train_path[0])
    test_data = dataPrep(test_path[1], test_path[0])

    train_iter = DataLoader(train_data, BATCH_SIZE, shuffle= True, collate_fn=generate_batch)
    test_iter = DataLoader(test_data, BATCH_SIZE, shuffle= True, collate_fn=generate_batch)

    INPUT_DIM = len(de_vocab)
    OUTPUT_DIM = len(en_vocab)
    ENC_EMB_DIM = 32
    DEC_EMB_DIM = 32
    ENC_HID_DIM = 64
    DEC_HID_DIM = 64
    ATTN_DIM = 8
    ENC_DROPOUT = 0.5
    DEC_DROPOUT = 0.5


    enc = Encoder(INPUT_DIM, ENC_EMB_DIM, ENC_HID_DIM, DEC_HID_DIM, ENC_DROPOUT)
    att = Attention(ENC_HID_DIM, DEC_HID_DIM, ATTN_DIM)
    dec= Decoder(OUTPUT_DIM, DEC_EMB_DIM, ENC_HID_DIM, DEC_HID_DIM, DEC_DROPOUT, att)
    model = Seq2Seq(enc, dec, device).to(device)

    def init_weights(m: nn.Module):
        for name, param in m.named_parameters():
            if 'weight' in name:
                nn.init.normal_(param.data, mean=0, std=0.01)
            else:
                nn.init.constant_(param.data, 0)


    model.apply(init_weights)
    optimizer = optim.Adam(model.parameters())

    def count_parameters(model: nn.Module):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)

    print(f'The model has {count_parameters(model):,} trainable parameters')
    
    PAD_IDX2 = en_vocab['<pad>']
    print( device)
    criterion = nn.CrossEntropyLoss(ignore_index=PAD_IDX2)

    N_EPOCHS = 5
    CLIP = 1

    if arg.mode == 'train':
        print("mode chosen: ", arg.mode)
        old_loss =0
        for epoch in range(N_EPOCHS):

            start_time = time.time()

            train_loss = train(model, train_iter, optimizer, criterion, CLIP)

            end_time = time.time()

            if old_loss > train_loss :
                torch.save(model.state_dict(), './model/s2s.pt')
            epoch_mins, epoch_secs = epoch_time(start_time, end_time)

            print(f'Epoch: {epoch+1:02} | Time: {epoch_mins}m {epoch_secs}s')
            print(f'\tTrain Loss: {train_loss:.3f} | Train PPL: {math.exp(train_loss):7.3f}')
            
    elif arg.mode == 'test':
        print("mode chosen: ", arg.mode)

        test_loss = evaluate(model, test_iter, criterion)
        print(f'| Test Loss: {test_loss:.3f} | Test PPL: {math.exp(test_loss):7.3f} |')

    # elif arg.mode == 'translate':
    #     print("mode chosen: ", arg.mode)
       