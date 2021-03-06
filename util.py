from __future__ import division

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
import cv2

# 获取任意给定图像中存在的类别
def unique(tensor):
    tensor_np =tensor.cpu().numpy()
    unique_np = np.unique(tensor_np)
    unique_tensor = torch.from_numpy(unique_np)

    tensor_res = tensor.new(unique_tensor.shape)
    tensor_res.copy_(unique_tensor)

    return tensor_res

# 计算两个边界框的IoU
def bbox_iou(box1, box2):
    # 获取边框的坐标
    b1_x1, b1_y1, b1_x2, b1_y2 = box1[:,0], box1[:,1], box1[:,2], box1[:,3]
    b2_x1, b2_y1, b2_x2, b2_y2 = box2[:,0], box2[:,1], box2[:,2], box2[:,3]

    # 获取交叉矩形的坐标
    inter_rect_x1 = torch.max(b1_x1, b2_x1)
    inter_rect_y1 = torch.max(b1_y1, b2_y1)
    inter_rect_x2 = torch.min(b1_x2, b2_x2)
    inter_rect_y2 = torch.min(b1_y2, b2_y2)

    # 交叉面积
    inter_area = torch.clamp(inter_rect_x2 - inter_rect_x1 + 1, min=0) * torch.clamp(inter_rect_y2 - inter_rect_y1 + 1, min=0)
   
    # 合并面积
    b1_area = (b1_x2 - b1_x1 + 1) * (b1_y2 - b1_y1 + 1)
    b2_area = (b2_x2 - b2_x1 + 1) * (b2_y2 - b2_y1 + 1)
    union_area = b1_area + b2_area - inter_area

    # IoU
    iou = inter_area / union_area

    return iou

# 把检测特征图转换成二维张量，张量的每一行对应边界框的属性，5个参数：输出，输入图像的维度……
def predict_transform(prediction, inp_dim, anchors, num_classes, CUDA=True):
    
    batch_size = prediction.size(0)
    stride = inp_dim // prediction.size(2)
    grid_size = inp_dim // stride
    bbox_attrs = 5 + num_classes
    num_anchors = len(anchors)

    prediction = prediction.view(batch_size, bbox_attrs*num_anchors, grid_size*grid_size)
    prediction = prediction.transpose(1,2).contiguous()
    prediction = prediction.view(batch_size, grid_size*grid_size*num_anchors, bbox_attrs)
    
    # 锚点的维度与net块的h和w属性一致，输入图像的维度和检测图的维度之商就是步长，用检测特征图的步长分割锚点
    anchors = [(a[0]/stride, a[1]/stride) for a in anchors]

    # 对（x，y）坐标和objectness分数执行Sigmoid函数操作
    prediction[:,:,0] = torch.sigmoid(prediction[:,:,0])
    prediction[:,:,1] = torch.sigmoid(prediction[:,:,1])
    prediction[:,:,4] = torch.sigmoid(prediction[:,:,4])

    # 将网格偏移添加到中心坐标预测中
    grid = np.arange(grid_size)
    a,b = np.meshgrid(grid, grid)

    x_offset = torch.FloatTensor(a).view(-1,1)
    y_offset = torch.FloatTensor(b).view(-1,1)

    if CUDA:
        x_offset = x_offset.cuda()
        y_offset = y_offset.cuda()
    
    x_y_offset = torch.cat((x_offset, y_offset), 1).repeat(1,num_anchors).view(-1,2).unsqueeze(0)

    prediction[:,:,:2] += x_y_offset

    # 将锚点应用到边界框维度中
    anchors = torch.FloatTensor(anchors)

    if CUDA:
        anchors = anchors.cuda()
    
    anchors = anchors.repeat(grid_size*grid_size, 1).unsqueeze(0)
    prediction[:,:,2:4] = torch.exp(prediction[:,:,2:4])*anchors

    # 将sigmoid激活函数应用到类别分数中
    prediction[:,:,5:5 + num_classes] = torch.sigmoid((prediction[:,:,5:5 + num_classes]))

    # 将检测图的大小调整到与输入图像大小一致，乘以stride变量（边界框属性根据特征图大小而定）
    prediction[:,:,:4] *= stride

    return prediction

# 加载类别，返回字典——将每个类别的索引映射到其名称的字符串
def load_classes(namesfile):
    fp = open(namesfile, "r")
    names = fp.read().split("\n")[:-1]
    return names

# 输出满足objectness分数阈值和非极大值抑制(NMS)，得到真实检测结果
def write_results(prediction, confidence, num_classes, nms_conf=0.4):
    # 输入为预测结果，置信度，类别数，NMS阈值

    # 低于objectness分数的每个边界框，其每个属性值都置0，即一整行。
    conf_mask = (prediction[:,:,4] > confidence).float().unsqueeze(2)
    prediction = prediction*conf_mask

    # 每个框的两个对焦坐标更容易计算两个框的IoU，故将(中心x，中心y，高度，宽度)属性转化成(左上角x，左上角y，右下角x，右下角y)
    box_a = prediction.new(prediction.shape)
    box_a[:,:,0] = (prediction[:,:,0] - prediction[:,:,2]/2)
    box_a[:,:,1] = (prediction[:,:,1] - prediction[:,:,3]/2)
    box_a[:,:,2] = (prediction[:,:,0] + prediction[:,:,2]/2)
    box_a[:,:,3] = (prediction[:,:,1] + prediction[:,:,3]/2)
    prediction[:,:,:4] = box_a[:,:,:4]

    batch_size = prediction.size(0)

    #output = prediction.new(1, prediction.size(2) + 1)
    write = False # 标识尚未初始化输出

    # 在第一个维度即bacth上循环，一次完成一个图像的置信度阈值和NMS
    for ind in range(batch_size):
        # 获取图像,10647x85
        image_pred = prediction[ind]

        # 每个边界框行有85个属性，其中80个类别分数，只取最大值的类别分数
        # 获取具有最高分数的类及其索引
        max_conf, max_conf_score = torch.max(image_pred[:,5:5+num_classes], 1)
        max_conf = max_conf.float().unsqueeze(1)
        max_conf_score = max_conf_score.float().unsqueeze(1)
        # 删除80个分类分数，增加最高分数类别的索引及最高分数
        seq = (image_pred[:,:5], max_conf, max_conf_score)
        image_pred = torch.cat(seq, 1)

        # 删除objectness置信度小于阈值的置0条目,try-except处理无检测结果的情况，continue跳过对本图像的循环
        non_zero_ind = torch.nonzero(image_pred[:,4])
        try:
            image_pred_ = image_pred[non_zero_ind.squeeze(),:].view(-1,7) # 7列
        except:
            continue
        
        # PyTorch 0.4兼容
        if image_pred_.shape[0] == 0:
            continue

        # 获得一个图像的所有种类
        img_classes = unique(image_pred_[:,-1])
        
        # 按类别执行NMS
        for cls in img_classes:
            # 得到一个类别的所有检测
            cls_mask = image_pred_*(image_pred_[:,-1] == cls).float().unsqueeze(1) 
            class_mask_ind = torch.nonzero(cls_mask[:,-2]).squeeze()

            image_pred_class = image_pred_[class_mask_ind].view(-1,7)

            # 对所有检测排序,按照objectness置信度
            conf_sort_index = torch.sort(image_pred_class[:,4], descending=True)[1]
            image_pred_class = image_pred_class[conf_sort_index]
            idx = image_pred_class.size(0)

            # 对于每一个检测,执行NMS
            for i in range(idx):
                # 获取正在查看的box之后所有boxes的IoUs
                try:
                    ious = bbox_iou(image_pred_class[i].unsqueeze(0), image_pred_class[i+1:])
                except ValueError: # image_pred_class[i+1,:]返回空张量
                    break
                except IndexError: # image_pred_class移除部分后，idx索引越界
                    break
                    
                # 清除IoU>阈值的检测
                iou_mask = (ious < nms_conf).float().unsqueeze(1)
                image_pred_class[i+1:] *= iou_mask

                # 移除0条目
                non_zero_ind = torch.nonzero(image_pred_class[:,4]).squeeze()
                image_pred_class = image_pred_class[non_zero_ind].view(-1,7)

            batch_ind = image_pred_class.new(image_pred_class.size(0), 1).fill_(ind)
            seq = batch_ind, image_pred_class

            if not write:
                output = torch.cat(seq, 1)
                write = True
            else:
                out = torch.cat(seq, 1)
                output = torch.cat((output, out))
    # 输出一个形状为Dx8的张量；其中D是所有图像中的「真实」检测结果，每个都用一行表示。
    # 每一个检测结果都有8个属性，即该检测结果所属的batch中图像的索引、4个对角的坐标、objectness分数、有最大置信度的类别的分数、该类别的索引。
    try:
        return output
    except:
        return 0

# 使用填充调整具有不变长宽性的图像
def letterbox_image(img, inp_dim):
    
    img_w, img_h = img.shape[1], img.shape[0]
    w, h = inp_dim
    new_w = int(img_w * min(w/img_w, h/img_h))
    new_h = int(img_h * min(w/img_w, h/img_h))
    resized_image = cv2.resize(img, (new_w,new_h), interpolation = cv2.INTER_CUBIC)
    
    canvas = np.full((inp_dim[1], inp_dim[0], 3), 128)

    canvas[(h-new_h)//2:(h-new_h)//2 + new_h,(w-new_w)//2:(w-new_w)//2 + new_w,  :] = resized_image
    
    return canvas

# 将numpy数组转换成PyTorch的的输入格式
# OpenCV将图像载入成numpy数组，颜色通道为BGR。
# PyTorch的图像输入格式是(batch x 通道 x 高度 x 宽度)，通道顺序RGB。
def prep_image(img, inp_dim):

    img = letterbox_image(img, (inp_dim, inp_dim)) # 转换格式大小
    img = img[:,:,::-1].transpose((2,0,1)).copy() # BGR -> RGB(起止位置省略，步长为-1，负:从右往左)) | H x W x C -> C x H x W
    img = torch.from_numpy(img).float().div(255.0).unsqueeze(0)

    return img
        


    