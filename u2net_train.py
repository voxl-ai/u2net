import os
import torch
import torchvision
import torch.quantization
from torch.autograd import Variable
import torch.nn as nn
import torch.cuda.amp as amp
import torch.nn.functional as F
from time import time
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, utils
import torch.optim as optim
import torchvision.transforms as standard_transforms

import numpy as np
import glob

from data_loader import Rescale
from data_loader import RescaleT
from data_loader import RandomCrop
from data_loader import ToTensor
from data_loader import ToTensorLab
from data_loader import SalObjDataset

from model import U2NET
from model import U2NETP

# ------- 1. define loss function --------

torch.backends.quantized.engine = "fbgemm"
bce_loss = nn.BCELoss(size_average=True)


def muti_bce_loss_fusion(d0, d1, d2, d3, d4, d5, d6, labels_v):

    loss0 = bce_loss(d0, labels_v)
    loss1 = bce_loss(d1, labels_v)
    loss2 = bce_loss(d2, labels_v)
    loss3 = bce_loss(d3, labels_v)
    loss4 = bce_loss(d4, labels_v)
    loss5 = bce_loss(d5, labels_v)
    loss6 = bce_loss(d6, labels_v)

    loss = loss0 + loss1 + loss2 + loss3 + loss4 + loss5 + loss6
    print(
        "l0: %3f, l1: %3f, l2: %3f, l3: %3f, l4: %3f, l5: %3f, l6: %3f"
        % (
            loss0.item(),
            loss1.item(),
            loss2.item(),
            loss3.item(),
            loss4.item(),
            loss5.item(),
            loss6.item(),
        )
    )

    return loss0, loss


def weight_init(m):
    if isinstance(m, nn.Conv2d):
        nn.init.kaiming_normal_(m.weight, mode="fan_out")
        if m.bias is not None:
            nn.init.zeros_(m.bias)
    elif isinstance(m, nn.BatchNorm2d):
        nn.init.ones_(m.weight)
        nn.init.zeros_(m.bias)
    elif isinstance(m, nn.Linear):
        nn.init.normal_(m.weight, 0, 0.01)
        nn.init.zeros_(m.bias)


if __name__ == "__main__":
    # ------- 2. set the directory of training dataset --------

    model_name = "u2netp"  #'u2net'

    data_dir = os.path.join(os.getcwd(), "data" + os.sep)
    tra_image_dir = os.path.join("DUTS-TR", "DUTS-TR-Image" + os.sep)
    tra_label_dir = os.path.join("DUTS-TR", "DUTS-TR-Mask" + os.sep)

    image_ext = ".jpg"
    label_ext = ".png"

    model_dir = os.path.join(os.getcwd(), "saved_models", model_name + os.sep)
    os.makedirs(model_dir, exist_ok=True)

    epoch_num = 100000
    batch_size_train = 8
    batch_size_val = 1
    train_num = 0
    val_num = 0

    tra_img_name_list = glob.glob(data_dir + tra_image_dir + "*" + image_ext)

    tra_lbl_name_list = []
    for img_path in tra_img_name_list:
        img_name = img_path.split(os.sep)[-1]

        aaa = img_name.split(".")
        bbb = aaa[0:-1]
        imidx = bbb[0]
        for i in range(1, len(bbb)):
            imidx = imidx + "." + bbb[i]

        tra_lbl_name_list.append(data_dir + tra_label_dir + imidx + label_ext)

    print("---")
    print("train images: ", len(tra_img_name_list))
    print("train labels: ", len(tra_lbl_name_list))
    print("---")

    train_num = len(tra_img_name_list)

    salobj_dataset = SalObjDataset(
        img_name_list=tra_img_name_list,
        lbl_name_list=tra_lbl_name_list,
        transform=transforms.Compose(
            [
                RescaleT(320),
                RandomCrop(288),
                ToTensorLab(flag=0),
            ]
        ),
    )
    salobj_dataloader = DataLoader(
        salobj_dataset, batch_size=batch_size_train, shuffle=True, num_workers=1
    )

    # ------- 3. define model --------
    # define the net
    if model_name == "u2net":
        net = U2NET(3, 1)
    else:  # elif model_name == "u2netp":
        net = U2NETP(3, 1)

    net.apply(weight_init)
    if torch.cuda.is_available():
        net.cuda()

    scaler = amp.GradScaler()
    net.qconfig = torch.quantization.default_qconfig
    net = torch.quantization.QuantWrapper(net)
    torch.quantization.prepare_qat(net, inplace=True)

    # ------- 4. define optimizer --------
    print("---define optimizer...")
    optimizer = optim.Adam(
        net.parameters(), lr=0.001, betas=(0.9, 0.999), eps=1e-08, weight_decay=0
    )

    # ------- 5. training process --------
    print("---start training...")
    ite_num = 0
    running_time = 0.0
    running_loss = 0.0
    running_tar_loss = 0.0
    ite_num4val = 0
    save_frq = 2000  # save the model every 2000 iterations

    for epoch in range(0, epoch_num):
        net.train()

        for i, data in enumerate(salobj_dataloader):
            ite_num = ite_num + 1
            ite_num4val = ite_num4val + 1

            inputs, labels = data["image"], data["label"]

            inputs = inputs.type(torch.FloatTensor)
            labels = labels.type(torch.FloatTensor)

            # wrap them in Variable
            if torch.cuda.is_available():
                inputs_v, labels_v = (
                    Variable(inputs.cuda(), requires_grad=False),
                    Variable(labels.cuda(), requires_grad=False),
                )
            else:
                inputs_v, labels_v = Variable(inputs, requires_grad=False), Variable(
                    labels, requires_grad=False
                )

            # y zero the parameter gradients
            optimizer.zero_grad()

            # forward + backward + optimize
            t0 = time()
            with amp.autocast():
                d0, d1, d2, d3, d4, d5, d6 = net(inputs_v)
            d0, d1, d2, d3, d4, d5, d6 = map(
                lambda x: x.float(), (d0, d1, d2, d3, d4, d5, d6)
            )
            loss2, loss = muti_bce_loss_fusion(d0, d1, d2, d3, d4, d5, d6, labels_v)

            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            # # print statistics
            running_time += time() - t0
            running_loss += loss.item()
            running_tar_loss += loss2.item()

            # del temporary outputs and loss
            del d0, d1, d2, d3, d4, d5, d6, loss2, loss

            if ite_num % 10 == 0:
                print()
                print(
                    "[epoch: %3d/%3d, batch: %5d/%5d, ite: %d] train loss: %3f, tar: %3f, step time: %3f"
                    % (
                        epoch + 1,
                        epoch_num,
                        (i + 1) * batch_size_train,
                        train_num,
                        ite_num,
                        running_loss / ite_num4val,
                        running_tar_loss / ite_num4val,
                        running_time / ite_num4val,
                    )
                )

            if ite_num % save_frq == 0:
                # net.cpu().eval()
                # torch.quantization.convert(net, inplace=True, remove_qconfig=True)
                torch.save(
                    net.state_dict(),
                    model_dir
                    + model_name
                    + "_bce_itr_%d_train_%3f_tar_%3f.pth"
                    % (
                        ite_num,
                        running_loss / ite_num4val,
                        running_tar_loss / ite_num4val,
                    ),
                )

                running_loss = 0.0
                running_tar_loss = 0.0
                net.train()  # resume train
                ite_num4val = 0
