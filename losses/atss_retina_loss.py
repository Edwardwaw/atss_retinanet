import torch
from utils.boxs import box_iou
from losses.common import IOULoss
from torch import nn
import math

INF = 1e8



class BoxCoder(object):
    def __init__(self, weights=None):
        super(BoxCoder, self).__init__()
        if weights is None:
            weights = [0.1, 0.1, 0.2, 0.2]
        self.weights = torch.tensor(data=weights, requires_grad=False)

    def encoder(self, anchors, gt_boxes):
        """
        :param gt_boxes:[box_num, 4]   4==> x1,y1,x2,y2
        :param anchors: [box_num, 4]   4==> x1,y1,x2,y2
        :return:
        """
        if self.weights.device != anchors.device:
            self.weights = self.weights.to(anchors.device)
        anchors_wh = anchors[..., [2, 3]] - anchors[..., [0, 1]]
        anchors_xy = anchors[..., [0, 1]] + 0.5 * anchors_wh
        gt_wh = (gt_boxes[..., [2, 3]] - gt_boxes[..., [0, 1]]).clamp(min=1.0)
        gt_xy = gt_boxes[..., [0, 1]] + 0.5 * gt_wh
        delta_xy = (gt_xy - anchors_xy) / anchors_wh
        delta_wh = (gt_wh / anchors_wh).log()

        delta_targets = torch.cat([delta_xy, delta_wh], dim=-1) / self.weights

        return delta_targets

    def decoder(self, predicts, anchors):
        """
        :param predicts: [anchor_num, 4] or [bs, anchor_num, 4]
        :param anchors: [anchor_num, 4]
        :return: [anchor_num, 4] (x1,y1,x2,y2)
        """
        if self.weights.device != anchors.device:
            self.weights = self.weights.to(anchors.device)
        anchors_wh = anchors[:, [2, 3]] - anchors[:, [0, 1]]
        anchors_xy = anchors[:, [0, 1]] + 0.5 * anchors_wh
        scale_reg = predicts * self.weights

        # Prevent sending too large values into torch.exp()
        # scale_reg[...,2:] = torch.clamp(scale_reg[...,2:],max=math.log(1000. / 16))

        scale_reg[..., :2] = anchors_xy + scale_reg[..., :2] * anchors_wh
        scale_reg[..., 2:] = scale_reg[..., 2:].exp() * anchors_wh
        scale_reg[..., :2] -= (0.5 * scale_reg[..., 2:])
        scale_reg[..., 2:] = scale_reg[..., :2] + scale_reg[..., 2:]

        return scale_reg


def smooth_l1_loss(predicts, target, beta=1. / 9):
    """
    very similar to the smooth_l1_loss from pytorch, but with
    the extra beta parameter
    """
    n = torch.abs(predicts - target)
    cond = n < beta
    loss = torch.where(cond, 0.5 * n ** 2 / beta, n - 0.5 * beta)
    return loss


class ATSSRetinaBuilder(object):
    def __init__(self, top_k=9, anchor_per_loc=1):
        super(ATSSRetinaBuilder, self).__init__()
        self.top_k = top_k
        self.anchor_per_loc = anchor_per_loc

    @torch.no_grad()
    def __call__(self, bs, anchors, targets):
        """
        :param bs: batch_size
        :param anchors: list(anchor) [all, 4] (x1,y1,x2,y2)
        :param targets: [gt_num, 7] (batch_id,weights,label_id,x1,y1,x2,y2)
        :return:
        """
        layer_num = len(anchors)
        num_anchor_per_layer = [len(anchor) for anchor in anchors]
        expand_anchors = torch.cat(anchors, dim=0)
        flag_list = list()
        targets_list = list()
        for bi in range(bs):
            # [b_gt_num, 6] (weights,label_id,x1,y1,x2,y2)
            batch_targets = targets[targets[:, 0] == bi, 1:]
            if len(batch_targets) == 0:
                flag_list.append(torch.zeros((len(expand_anchors),), device=expand_anchors.device, dtype=torch.bool))
                targets_list.append(torch.Tensor())
                continue
            anchor_gt_iou = box_iou(expand_anchors, batch_targets[:, 2:])
            pos_ids = torch.zeros(size=anchor_gt_iou.shape, dtype=torch.bool, device=expand_anchors.device)
            anchor_xy = (expand_anchors[:, [0, 1]] + expand_anchors[:, [2, 3]]) / 2.
            gt_xy = (batch_targets[:, [2, 3]] + batch_targets[:, [4, 5]]) / 2.

            left = anchor_xy[:, None, 0] - batch_targets[None, :, 2]
            top = anchor_xy[:, None, 1] - batch_targets[None, :, 3]
            right = batch_targets[None, :, 4] - anchor_xy[:, None, 0]
            bottom = batch_targets[None, :, 5] - anchor_xy[:, None, 1]
            is_in_gts = torch.stack([left, top, right, bottom], dim=2).min(dim=2)[0] > 0.01
            anchor_gt_distance = ((anchor_xy[:, None, :] - gt_xy[None, :, :]) ** 2).sum(-1).sqrt()
            start = 0
            candidate_list = list()
            for li in range(layer_num):
                current_anchor_num = num_anchor_per_layer[li]
                end = start + current_anchor_num
                current_anchor_gt_distance = anchor_gt_distance[start:end, :]
                layer_top_k = min(self.top_k * self.anchor_per_loc, current_anchor_num)
                _, near_anchor_idx = current_anchor_gt_distance.topk(layer_top_k, dim=0, largest=False)
                candidate_list.append(near_anchor_idx + start)
                start = end
            candidate_idx = torch.cat(candidate_list, dim=0)
            candidate_ious = anchor_gt_iou.gather(0, candidate_idx)
            gt_iou_thresh = candidate_ious.mean(dim=0) + candidate_ious.std(dim=0)
            candidate_pos_bool = candidate_ious >= gt_iou_thresh
            pos_ids.scatter_(dim=0, index=candidate_idx, src=candidate_pos_bool)
            valid_ids = pos_ids & is_in_gts
            anchor_gt_iou[~valid_ids] = -INF
            max_val, max_gt_idx = anchor_gt_iou.max(dim=1)
            flag = (max_val != -INF)
            gt_targets = batch_targets[max_gt_idx, :]
            flag_list.append(flag)
            targets_list.append(gt_targets)

        return flag_list, targets_list, expand_anchors


class ATSSRetinaLoss(object):
    def __init__(self, top_k=9, anchor_per_loc=1, alpha=0.25, gamma=2.0, beta=1. / 9):
        self.alpha = alpha
        self.gama = gamma
        self.beta = beta
        self.builder = ATSSRetinaBuilder(top_k, anchor_per_loc)
        self.center_loss=nn.BCEWithLogitsLoss(reduction='sum')
        self.reg_loss_func=IOULoss('giou','xyxy')
        self.std = torch.tensor([0.1, 0.1, 0.2, 0.2]).float()
        self.box_coder=BoxCoder()

    def compute_centerness_targets(self, reg_targets, pred_center):
        l = pred_center[:,0] - reg_targets[:, 0]
        t = pred_center[:,1] - reg_targets[:, 1]
        r = reg_targets[:, 2] - pred_center[:,0]
        b = reg_targets[:, 3] - pred_center[:,1]
        left_right = torch.stack([l, r], dim=1)
        top_bottom = torch.stack([t, b], dim=1)
        centerness = (left_right.min(dim=-1)[0] / left_right.max(dim=-1)[0]) * (top_bottom.min(dim=-1)[0] / top_bottom.max(dim=-1)[0])
        minval,max_val=centerness.min(),centerness.max()
        if minval<0 or max_val>1:
            print('debug',minval,max_val )
        return torch.sqrt(centerness)

    def __call__(self, cls_predicts, reg_predicts, center_predicts, anchors, targets):
        """
        :param cls_predicts:
        :param reg_predicts:
        :param anchors:
        :param targets:
        :return:
        """
        for i in range(len(cls_predicts)):
            if cls_predicts[i].dtype == torch.float16:
                cls_predicts[i] = cls_predicts[i].float()

        device = cls_predicts[0].device
        bs = cls_predicts[0].shape[0]

        flags, gt_targets, all_anchors = self.builder(bs, anchors, targets)
        anchors_wh = all_anchors[:, [2, 3]] - all_anchors[:, [0, 1]]
        anchors_xy = all_anchors[:, [0, 1]] + 0.5 * anchors_wh
        std = self.std.to(device)

        cls_loss_list = list()
        reg_loss_list = list()
        centerness_loss_list=list()

        pos_num_sum = 0
        sum_centerness_targets=0

        for bi in range(bs):
            batch_cls_predict = torch.cat([cls_item[bi] for cls_item in cls_predicts], dim=0).sigmoid().clamp(1e-6, 1 - 1e-6)
            batch_reg_predict = torch.cat([reg_item[bi] for reg_item in reg_predicts], dim=0)
            batch_center_predict = torch.cat([center_item[bi].view(-1) for center_item in center_predicts], dim=0)


            flag = flags[bi]
            pos_idx = flag.nonzero(as_tuple=False).squeeze(1)
            gt = gt_targets[bi]
            pos_num = len(pos_idx)
            if pos_num == 0:
                neg_cls_loss = -(1 - self.alpha) * batch_cls_predict ** self.gama * ((1 - batch_cls_predict).log())
                cls_loss_list.append(neg_cls_loss.sum())
                continue
            pos_num_sum += pos_num

            ## loss 1: focal loss
            cls_targets = torch.zeros(size=batch_cls_predict.shape, device=device)
            cls_targets[pos_idx, gt[pos_idx, 1].long()] = 1.
            pos_loss = -self.alpha * cls_targets * ((1 - batch_cls_predict) ** self.gama) * batch_cls_predict.log()
            neg_loss = -(1 - self.alpha) * (1. - cls_targets) * (batch_cls_predict ** self.gama) * (
                (1 - batch_cls_predict).log())
            cls_loss = (pos_loss + neg_loss).sum()
            cls_loss_list.append(cls_loss)


            valid_reg_predicts = batch_reg_predict[pos_idx, :]
            gt_bbox = gt[pos_idx, 2:]

            valid_anchor_wh = anchors_wh[pos_idx, :]
            valid_anchor_xy = anchors_xy[pos_idx, :]

            valid_center_predicts=batch_center_predict[pos_idx]
            center_targets=self.compute_centerness_targets(gt_bbox,valid_anchor_xy)

            sum_centerness_targets+=center_targets.sum()


            decode_valid_reg_predicts = self.box_coder.decoder(valid_reg_predicts,all_anchors[pos_idx])
            # gt_wh = (gt_bbox[:, [2, 3]] - gt_bbox[:, [0, 1]]).clamp(min=1.0)
            # gt_xy = gt_bbox[:, [0, 1]] + 0.5 * gt_wh
            #
            # delta_xy = (gt_xy - valid_anchor_xy) / valid_anchor_wh
            # delta_wh = (gt_wh / valid_anchor_wh).log()

            # delta_targets = torch.cat([delta_xy, delta_wh], dim=-1) / std
            # reg_loss = smooth_l1_loss(valid_reg_predicts, delta_targets, beta=self.beta).sum(dim=-1)
            reg_loss=self.reg_loss_func(decode_valid_reg_predicts,gt_bbox)
            reg_loss_list.append((reg_loss*center_targets).sum())



            center_loss=self.center_loss(valid_center_predicts,center_targets)
            centerness_loss_list.append(center_loss)


        cls_loss_sum = torch.stack(cls_loss_list).sum()
        if pos_num_sum == 0:
            total_loss = cls_loss_sum / bs
            return total_loss, torch.stack([cls_loss_sum, torch.tensor(data=0., device=device), torch.tensor(data=0., device=device)]).detach(), pos_num_sum
        reg_loss_sum = torch.stack(reg_loss_list).sum()
        center_loss_sum=torch.stack(centerness_loss_list).sum()

        cls_loss_mean = cls_loss_sum / pos_num_sum
        reg_loss_mean = reg_loss_sum / sum_centerness_targets
        center_loss_mean = center_loss_sum/pos_num_sum
        total_loss = cls_loss_mean + 2.0 * reg_loss_mean + center_loss_mean

        return total_loss, torch.stack([cls_loss_mean, reg_loss_mean, center_loss_mean]).detach(), pos_num_sum
