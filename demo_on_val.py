import gc
import os
from glob import glob
import numpy as np
from PIL import Image
import pickle
from tqdm import tqdm_notebook, tqdm
from models.network import U_Net, R2U_Net, AttU_Net, R2AttU_Net
from models.linknet import LinkNet34
from models.deeplabv3.deeplabv3plus import DeepLabV3Plus
from backboned_unet import Unet
import segmentation_models_pytorch as smp
from torchvision import transforms
import cv2
from albumentations import CLAHE
import json
from models.Transpose_unet.unet.model import Unet as Unet_t
from models.octave_unet.unet.model import OctaveUnet
from sklearn.model_selection import KFold, StratifiedKFold
import matplotlib.pyplot as plt
import copy
import torch

class Test(object):
    def __init__(self, model_type, image_size, mean, std, t=None):
        # Models
        self.unet = None
        self.image_size = image_size # 模型的输入大小

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model_type = model_type
        self.t = t
        self.mean = mean
        self.std = std

    def build_model(self):
        """Build generator and discriminator."""
        if self.model_type == 'U_Net':
            self.unet = U_Net(img_ch=3, output_ch=1)
        elif self.model_type == 'AttU_Net':
            self.unet = AttU_Net(img_ch=3, output_ch=1)

        elif self.model_type == 'unet_resnet34':
            # self.unet = Unet(backbone_name='resnet34', classes=1)
            self.unet = smp.Unet('resnet34', encoder_weights='imagenet', activation=None)
        elif self.model_type == 'unet_resnet50':
            self.unet = smp.Unet('resnet50', encoder_weights='imagenet', activation=None)
        elif self.model_type == 'unet_se_resnext50_32x4d':
            self.unet = smp.Unet('se_resnext50_32x4d', encoder_weights='imagenet', activation=None)
        elif self.model_type == 'unet_densenet121':
            self.unet = smp.Unet('densenet121', encoder_weights='imagenet', activation=None)
        elif self.model_type == 'unet_resnet34_t':
            self.unet = Unet_t('resnet34', encoder_weights='imagenet', activation=None, use_ConvTranspose2d=True)
        elif self.model_type == 'unet_resnet34_oct':
            self.unet = OctaveUnet('resnet34', encoder_weights='imagenet', activation=None)

        elif self.model_type == 'pspnet_resnet34':
            self.unet = smp.PSPNet('resnet34', encoder_weights='imagenet', classes=1, activation=None)
        elif self.model_type == 'linknet':
            self.unet = LinkNet34(num_classes=1)
        elif self.model_type == 'deeplabv3plus':
            self.unet = DeepLabV3Plus(model_backbone='res50_atrous', num_classes=1)
            # self.unet = DeepLabV3Plus(num_classes=1)

        # print('build model done！')

        self.unet.to(self.device)

    def test_model(
        self, 
        thresholds_classify, 
        thresholds_seg, 
        average_threshold,
        stage_cla, 
        stage_seg, 
        n_splits, 
        test_best_model=True, 
        less_than_sum=2048*2,
        seg_average_vote=True, 
        images_path=None, 
        masks_path=None
        ):
        """

        Args:
            thresholds_classify: list, 各个分类模型的阈值，高于这个阈值的置为1，否则置为0
            thresholds_seg: list，各个分割模型的阈值
            average_threshold: 分割后使用平均策略时所使用的平均阈值
            stage_cla: 第几阶段的权重作为分类结果
            stage_seg: 第几阶段的权重作为分割结果
            n_splits: list, 测试哪几折的结果进行平均
            test_best_model: 是否要使用最优模型测试，若不是的话，则取最新的模型测试
            less_than_sum: list, 预测图片中有预测出的正样本总和小于这个值时，则忽略所有
            seg_average_vote: bool，True：平均，False：投票
        """

        # 对于每一折加载模型，对所有测试集测试，并取平均

        with torch.no_grad():
            for index, (image_path, mask_path) in enumerate(tqdm(zip(images_path, masks_path), total=len(images_path))):
                img = Image.open(image_path).convert('RGB')
                pred_nfolds = 0
                for fold in n_splits:
                    # 加载分类模型，进行测试
                    self.unet = None
                    self.build_model()
                    if test_best_model:
                        unet_path = os.path.join('checkpoints', self.model_type,
                                                 self.model_type + '_{}_{}_best.pth'.format(stage_cla, fold))
                    else:
                        unet_path = os.path.join('checkpoints', self.model_type,
                                                 self.model_type + '_{}_{}.pth'.format(stage_cla, fold))
                    # print("Load classify weight from %s" % unet_path)
                    self.unet.load_state_dict(torch.load(unet_path)['state_dict'])
                    self.unet.eval()

                    seg_unet = copy.deepcopy(self.unet)
                    # 加载分割模型，进行测试s
                    if test_best_model:
                        unet_path = os.path.join('checkpoints', self.model_type,
                                                 self.model_type + '_{}_{}_best.pth'.format(stage_seg, fold))
                    else:
                        unet_path = os.path.join('checkpoints', self.model_type,
                                                 self.model_type + '_{}_{}.pth'.format(stage_seg, fold))
                    # print('Load segmentation weight from %s.' % unet_path)
                    seg_unet.load_state_dict(torch.load(unet_path)['state_dict'])
                    seg_unet.eval()

                    pred = self.tta(img, self.unet)

                    # 首先经过阈值和像素阈值，判断该图像中是否有掩模
                    pred = np.where(pred > thresholds_classify[fold], 1, 0)
                    if np.sum(pred) < less_than_sum[fold]:
                        pred[:] = 0

                    # 如果有掩膜的话，加载分割模型进行测试
                    if np.sum(pred) > 0:
                        pred = self.tta(img, seg_unet)
                        # 如果不是采用平均策略，即投票策略，则进行阈值处理，变成0或1
                        if not seg_average_vote:
                            pred = np.where(pred > thresholds_seg[fold], 1, 0)
                    pred_nfolds += pred

                if not seg_average_vote:
                    vote_model_num = len(n_splits)
                    vote_ticket = round(vote_model_num / 2.0)
                    pred = np.where(pred_nfolds > vote_ticket, 1, 0)
                    # print("Using voting strategy, Ticket / Vote models: %d / %d" % (vote_ticket, vote_model_num))
                else:
                    # print('Using average strategy.')
                    pred = pred_nfolds / len(n_splits)
                    pred = np.where(pred > average_threshold, 1, 0)

                pred = cv2.resize(pred, (1024, 1024))
                mask = Image.open(mask_path)
                mask = np.around(np.array(mask.convert('L'))/256.)

                self.combine_display(img, mask, pred, 'demo')

    def image_transform(self, image):
        """对样本进行预处理
        """
        resize = transforms.Resize(self.image_size)
        to_tensor = transforms.ToTensor()
        normalize = transforms.Normalize(self.mean, self.std)

        transform_compose = transforms.Compose([resize, to_tensor, normalize])

        return transform_compose(image)
    
    def detection(self, image, model):
        """对输入样本进行检测
        
        Args:
            image: 待检测样本，Image
            model: 要使用的网络
        Return:
            pred: 检测结果
        """
        image = self.image_transform(image)
        image = torch.unsqueeze(image, dim=0)
        image = image.float().to(self.device)
        pred = torch.sigmoid(model(image))
        # 预测出的结果
        pred = pred.view(self.image_size, self.image_size)
        pred = pred.detach().cpu().numpy()

        return pred
    
    def tta(self, image, model):
        """执行TTA预测

        Args:
            image: Image图片
            model: 要使用的网络
        Return:
            pred: 最后预测的结果
        """
        preds = np.zeros([self.image_size, self.image_size])
        # 768大小
        # image_resize = image.resize((768, 768))
        # resize_pred = self.detection(image_resize)
        # resize_pred_img = Image.fromarray(resize_pred)
        # resize_pred_img = resize_pred_img.resize((1024, 1024))
        # preds += np.asarray(resize_pred_img)

        # 左右翻转
        image_hflip = image.transpose(Image.FLIP_LEFT_RIGHT)

        hflip_pred = self.detection(image_hflip, model)
        hflip_pred_img = Image.fromarray(hflip_pred)
        pred_img = hflip_pred_img.transpose(Image.FLIP_LEFT_RIGHT)
        preds += np.asarray(pred_img)

        # CLAHE
        aug = CLAHE(p=1.0)
        image_np = np.asarray(image)
        clahe_image = aug(image=image_np)['image']
        clahe_image = Image.fromarray(clahe_image)
        clahe_pred = self.detection(clahe_image, model)
        preds += clahe_pred

        # 原图
        original_pred = self.detection(image, model)
        preds += original_pred

        # 求平均
        pred = preds / 3.0

        return pred

    # dice for threshold selection
    def dice_overall(self, preds, targs):
        n = preds.shape[0]  # batch size为多少
        preds = preds.view(n, -1)
        targs = targs.view(n, -1)
        # preds, targs = preds.to(self.device), targs.to(self.device)
        preds, targs = preds.cpu(), targs.cpu()

        # tensor之间按位相成，求两个集合的交(只有1×1等于1)后。按照第二个维度求和，得到[batch size]大小的tensor，每一个值代表该输入图片真实类标与预测类标的交集大小
        intersect = (preds * targs).sum(-1).float()
        # tensor之间按位相加，求两个集合的并。然后按照第二个维度求和，得到[batch size]大小的tensor，每一个值代表该输入图片真实类标与预测类标的并集大小
        union = (preds + targs).sum(-1).float()
        '''
        输入图片真实类标与预测类标无并集有两种情况：第一种为预测与真实均没有类标，此时并集之和为0；第二种为真实有类标，但是预测完全错误，此时并集之和不为0;

        寻找输入图片真实类标与预测类标并集之和为0的情况，将其交集置为1，并集置为2，最后还有一个2*交集/并集，值为1；
        其余情况，直接按照2*交集/并集计算，因为上面的并集并没有减去交集，所以需要拿2*交集，其最大值为1
        '''
        u0 = union == 0
        intersect[u0] = 1
        union[u0] = 2
        
        return (2. * intersect / union).mean()

    def combine_display(self, image_raw, mask, pred, title_diplay):
        plt.suptitle(title_diplay)
        plt.subplot(1, 3, 1)
        plt.title('image_raw')
        plt.imshow(image_raw)

        plt.subplot(1, 3, 2)
        plt.title('mask')
        plt.imshow(mask)

        plt.subplot(1, 3, 3)
        plt.title('pred')
        plt.imshow(pred)

        plt.show()

if __name__ == "__main__":
    mean = (0.485, 0.456, 0.406)
    std = (0.229, 0.224, 0.225)
    # mean = (0.490, 0.490, 0.490)
    # std = (0.229, 0.229, 0.229)
    model_name = 'unet_resnet34'
    # stage_cla表示使用第几阶段的权重作为分类模型，stage_seg表示使用第几阶段的权重作为分割模型，对应不同的image_size，index表示为交叉验证的第几个
    # image_size TODO
    stage_cla, stage_seg = 2, 3
    
    if stage_cla == 1:
        image_size = 768
    elif stage_cla == 2:
        image_size = 1024
    
    with open('checkpoints/'+model_name+'/result_stage2.json', 'r', encoding='utf-8') as json_file:
        config_cla = json.load(json_file)
    
    with open('checkpoints/'+model_name+'/result_stage3.json', 'r', encoding='utf-8') as json_file:
        config_seg = json.load(json_file)
    
    n_splits = [0] # 0, 1, 2, 3, 4
    thresholds_classify, thresholds_seg, less_than_sum = [0 for x in range(5)], [0 for x in range(5)], [0 for x in range(5)]
    for x in n_splits:
        thresholds_classify[x] = config_cla[str(x)][0]
        less_than_sum[x] = config_cla[str(x)][1]
        thresholds_seg[x] = config_seg[str(x)][0]
    seg_average_vote = False
    average_threshold = np.sum(np.asarray(thresholds_seg))/len(n_splits)
    test_best_mode = True
    
    print("stage_cla: %d, stage_seg: %d" % (stage_cla, stage_seg))
    print('test fold: ', n_splits)
    print('thresholds_classify: ', thresholds_classify)
    if seg_average_vote:
        print('Using average stategy, average_threshold: %f' % average_threshold)
    else:
        print('Using vating strategy, thresholds_seg: ', thresholds_seg)
    print('less_than_sum: ', less_than_sum)

    # 只有test的样本路径
    with open('dataset_static_mask.pkl', 'rb') as f:
        static = pickle.load(f)
        images_path, masks_path, masks_bool = static[0], static[1], static[2]
    # 只有stage1的训练集的样本路径
    with open('dataset_static_mask_stage1.pkl', 'rb') as f:
        static_stage1 = pickle.load(f)
        images_path_stage1, masks_path_stage1, masks_bool_stage1 = static_stage1[0], static_stage1[1], static_stage1[2]
    
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=1)
    split = skf.split(images_path, masks_bool)
    split_stage1 = skf.split(images_path_stage1, masks_bool_stage1)

    val_image_nfolds = list()
    val_mask_nfolds = list()
    for index, ((train_index, val_index), (train_stage1_index, val_stage1_index)) in enumerate(zip(split, split_stage1)):
        val_image = [images_path[x] for x in val_index]
        val_mask = [masks_path[x] for x in val_index]

        val_image_stage1 = [images_path_stage1[x] for x in val_stage1_index]
        val_mask_stage1 = [masks_path_stage1[x] for x in val_stage1_index]

        val_image_fold = val_image + val_image_stage1
        val_mask_fold = val_mask + val_mask_stage1

        val_image_nfolds.append(val_image_fold)
        val_mask_nfolds.append(val_mask_fold)

    val_image_fold0 = val_image_nfolds[0]
    val_mask_fold0 = val_mask_nfolds[0]

    solver = Test(model_name, image_size, mean, std)
    solver.test_model(
        thresholds_classify=thresholds_classify,
        thresholds_seg=thresholds_seg,
        average_threshold=average_threshold, 
        stage_cla=stage_cla,
        stage_seg=stage_seg, 
        n_splits=n_splits, 
        test_best_model=test_best_mode, 
        less_than_sum=less_than_sum,
        seg_average_vote=seg_average_vote, 
        images_path=images_path, 
        masks_path=masks_path
        )