import os
from tqdm import tqdm
import torch
import nibabel as nb
import numpy as np
from typing import Iterable
import logging
from einops import rearrange
import torch.nn.functional as F
import Joint_motion_seg_estimate_CMR.functions_collection as ff


def train_loop(args, model, data_loader_train, optimizer):

    model.train(True)

    # define loss
    if args.turn_zero_seg_slice_into is not None:
        # print('ignore index: ', args.turn_zero_seg_slice_into)
        seg_criterion = torch.nn.CrossEntropyLoss(ignore_index = 10)
    else:
        seg_criterion = torch.nn.CrossEntropyLoss()

    loss_list = []
    ce_loss_list = []
    dice_loss_list = []
    start_to_have_zero = False

    assert len(data_loader_train) == len(args.dataset_train)
    previous_loss = np.inf

    for i in range(len(data_loader_train)):
        current_data_loader = data_loader_train[i] # this is an iterable
        current_dataset_name = args.dataset_train[i][0]
        current_slice_type = args.dataset_train[i][1]
        print('in training current slice type: ', current_slice_type, ' current dataset name: ', current_dataset_name)

        for batch_idx, batch in enumerate(current_data_loader, 1):

            with torch.cuda.amp.autocast():
                if batch_idx == 1 or batch_idx % args.accum_iter == 0 or batch_idx == len(data_loader_train):
                    optimizer.zero_grad()
            
                # image
                batch_image = batch['image']
                image_input = torch.clone(batch_image).to("cuda")

                # segmentation
                batch_seg = batch['mask']
                batch_seg = rearrange(batch_seg, 'b c h w d -> (b d) c h w').to("cuda")

                seg_pred = model(image_input)
                seg_pred = rearrange(seg_pred, 'b c h w d -> (b d) c h w')
    
                # CE loss
                ce_loss = seg_criterion(seg_pred, torch.clone(batch_seg).squeeze(1).long()) 

                # Dice loss
                dice_loss = ff.customized_dice_loss(seg_pred,torch.clone(batch_seg).squeeze(1).long(), num_classes = args.num_classes, exclude_index = args.turn_zero_seg_slice_into)

                loss = args.loss_weight[0] * ce_loss + args.loss_weight[1] * dice_loss
                
                # check output 
                pred_softmax = F.softmax(seg_pred,dim = 1)
                pred_seg_softmax = pred_softmax.argmax(1).detach().cpu().numpy()
                if np.unique(pred_seg_softmax).shape[0] != 2 or (torch.unique(batch_seg).shape[0] != 2 and torch.unique(batch_seg).shape[0] != 3):
                    print('WRONG!!! unique pred_seg_softmax: ', np.unique(pred_seg_softmax), ' unique in seg_gt_CE: ', torch.unique(batch_seg))

                if torch.isinf(loss).any() or torch.isnan(loss).any():
                    print("Inf or NaN in loss, use previous loss!!!!")
                    loss = previous_loss
                previous_loss = loss

                if batch_idx == 1 or batch_idx % args.accum_iter == 0 or batch_idx == len(current_data_loader):
                    loss.backward()
                    optimizer.step()

                if batch_idx % 200 == 0 and batch_idx != 0:
                    print('in this iteration loss: ', np.round(sum(loss_list) / len(loss_list),3), ' ce loss: ', np.round(sum(ce_loss_list) / len(ce_loss_list),3), ' dice loss: ', np.round(sum(dice_loss_list) / len(dice_loss_list),3))
        

            loss_list.append(loss.item())
            ce_loss_list.append(ce_loss.item())
            dice_loss_list.append(dice_loss.item())


    return [sum(loss_list) / len(loss_list), sum(ce_loss_list) / len(ce_loss_list), sum(dice_loss_list) / len(dice_loss_list)]
