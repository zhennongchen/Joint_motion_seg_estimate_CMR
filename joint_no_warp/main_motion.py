#!/usr/bin/env python
import torch
import torch.optim as optim
from torch.autograd import Variable
import torch.nn as nn
import numpy as np
from tqdm import tqdm
import random
from torch.utils.data import DataLoader
import scipy.io
import os
import pdb
import argparse
import pandas as pd
from einops import rearrange

from Joint_motion_seg_estimate_CMR.pytorch.network import *
from Joint_motion_seg_estimate_CMR.data.data_CMR import *
from Joint_motion_seg_estimate_CMR.pytorch.util import *
from Joint_motion_seg_estimate_CMR.pytorch.train_engine import *
from Joint_motion_seg_estimate_CMR.pytorch.validate_engine import *
import Joint_motion_seg_estimate_CMR.Defaults as Defaults
import Joint_motion_seg_estimate_CMR.functions_collection as ff

defaults = Defaults.Parameters()

def get_args_parser():
    parser = argparse.ArgumentParser('joint', add_help=True)
    parser.add_argument('--batch_size', default=1, type=int, help='Batch size per GPU (effective batch size is batch_size * accum_iter * # gpus')
    
    # Optimizer parameters
    parser.add_argument('--lr', type=float, default=1e-4, metavar='LR', help='base learning rate: absolute_lr = base_lr * total_batch_size / 256')

    
    # Custom parser 
    parser.add_argument('--device', default='cuda', help='device to use for training / testing')
    parser.add_argument('--seed', default=1234, type=int)   
    
    
    ########## important parameters
    trial_name = 'joint_trial1'
    main_save_model = os.path.join(defaults.sam_dir, 'models', trial_name)
    pretrained_model_epoch = 2

    parser.add_argument('--output_dir', default = main_save_model, help='path where to save, empty for no saving')
    parser.add_argument('--pretrained_model_epoch', default = pretrained_model_epoch)

    if pretrained_model_epoch == None:
        parser.add_argument('--pretrained_model', default = None, help='path where to save, empty for no saving')
    else:
        parser.add_argument('--pretrained_model', default = os.path.join(main_save_model, 'models', 'model-%s.pth' % pretrained_model_epoch), help='path where to save, empty for no saving')

    parser.add_argument('--train_mode', default=True)
    parser.add_argument('--validation', default=True)
    parser.add_argument('--save_prediction', default=True)

    if pretrained_model_epoch == None:
        parser.add_argument('--start_epoch', default=1, type=int, metavar='N', help='start epoch')
    else:
        parser.add_argument('--start_epoch', default=pretrained_model_epoch+1, type=int, metavar='N', help='start epoch')
    parser.add_argument('--epochs', default=2, type=int)
    parser.add_argument('--save_model_file_every_N_epoch', default=1, type = int) 
    parser.add_argument('--lr_update_every_N_epoch', default=2, type = int)
    parser.add_argument('--lr_decay_gamma', default=0.95)
    
    # Dataset parameters
    parser.add_argument('--img_size', default=128, type=int)    
    parser.add_argument('--num_classes', type=int, default=2)  ######## important!!!!

    parser.add_argument('--dataset_name', default='STACOM')
    parser.add_argument('--full_or_nonzero_slice', default='nonzero') # full means all the slices, nonzero means only the slices with manual segmentation at both ED and ES, loose means the slices with manual segmentation at either ED or ES or both
    parser.add_argument('--turn_zero_seg_slice_into', default=10, type=int)
    parser.add_argument('--augment_list', default=[('brightness' , None), ('contrast', None), ('sharpness', None), ('flip', None), ('rotate', [-20,20]), ('translate', [-5,5]), ('random_crop', [-5,5])], type=list)
    parser.add_argument('--augment_frequency', default=0.5, type=float)

    return parser


def run(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # build some folders
    ff.make_folder([args.output_dir, os.path.join(args.output_dir, 'models'), os.path.join(args.output_dir, 'logs')])

    # Data loading code
    train_index_list = np.arange(0,2,1)  
    valid_index_list = np.arange(0,2,1) # just to monitor the validation loss, will not be used to select any hyperparameters
    train_batch_list = None
    valid_batch_list = None

    dataset_train = build_data_CMR(args, args.dataset_name, 
                    train_batch_list,  train_index_list, full_or_nonzero_slice = args.full_or_nonzero_slice,
                    shuffle = True,
                    augment_list = args.augment_list, augment_frequency = args.augment_frequency,
                    return_arrays_or_dictionary = 'dictionary')
    
    valid_batch_list = build_data_CMR(args, args.dataset_name,
                    valid_batch_list, valid_index_list, full_or_nonzero_slice = args.full_or_nonzero_slice,
                    shuffle = False,
                    augment_list = [], augment_frequency = -0.1,
                    return_arrays_or_dictionary = 'dictionary')

    data_loader_train = torch.utils.data.DataLoader(dataset_train, batch_size = 1, shuffle = False, pin_memory = True, num_workers = 0)# cpu_count()) 
    data_loader_valid = torch.utils.data.DataLoader(valid_batch_list, batch_size = 1, shuffle = False, pin_memory = True, num_workers = 0)# cpu_count())

    # build model
    model = Registration_Net()
    model = model.to(device)
    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()),lr=args.lr)
    # load pretrained model
    if args.pretrained_model is not None:
        print('loading pretrained model from: ', args.pretrained_model)
        checkpoint = torch.load(args.pretrained_model)
        model.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        print('loaded pretrained model from: ', args.pretrained_model)


    # train loop
    training_log = []

    for epoch in range(args.start_epoch, args.start_epoch + args.epochs):
        print('training epoch:', epoch)

        # update learning rate
        if epoch % args.lr_update_every_N_epoch == 0:
            optimizer.param_groups[0]['lr'] *= args.lr_decay_gamma
        print('learning rate now: ', optimizer.param_groups[0]['lr'])

        epoch_loss =  train_loop_motion(args, model, data_loader_train, optimizer)
        
        # on_epoch_end
        dataset_train.on_epoch_end()

        print('end of epoch: ', epoch, 'average loss: ', epoch_loss)
        # save model
        if epoch % args.save_model_file_every_N_epoch == 0:
            checkpoint_path = os.path.join(args.output_dir, 'models', 'model-%s.pth' % epoch)
            to_save = {'model': model.state_dict(),
                        'optimizer': optimizer.state_dict(),
                        'epoch': epoch,
                        'args': args,}
            torch.save(to_save, checkpoint_path)

        # validate
        if epoch % args.save_model_file_every_N_epoch == 0 and args.validation == True:
            valid_loss = valid_loop_motion(args, model, data_loader_valid)
            print('validation loss: ', valid_loss)


        # save_log
        training_log.append([epoch, epoch_loss, optimizer.param_groups[0]['lr'], valid_loss])
        df = pd.DataFrame(training_log, columns = ['epoch', 'loss', 'lr', 'valid_loss'])
        df.to_excel(os.path.join(args.output_dir, 'logs', 'training_log.xlsx'))



if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()
    print(args)
    run(args)

