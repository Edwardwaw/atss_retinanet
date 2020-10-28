import torch
import math



class BoxSimilarity(object):
    def __init__(self, iou_type="giou", coord_type="xyxy", eps=1e-9):
        self.iou_type = iou_type
        self.coord_type = coord_type
        self.eps = eps

    def __call__(self, box1, box2):
        """
        :param box1: [num,4] predicts
        :param box2:[num,4] targets
        :return:
        """
        box1_t = box1.T
        box2_t = box2.T

        if self.coord_type == "xyxy":
            b1_x1, b1_y1, b1_x2, b1_y2 = box1_t[0], box1_t[1], box1_t[2], box1_t[3]
            b2_x1, b2_y1, b2_x2, b2_y2 = box2_t[0], box2_t[1], box2_t[2], box2_t[3]
        elif self.coord_type == "xywh":
            b1_x1, b1_x2 = box1_t[0] - box1_t[2] / 2., box1_t[0] + box1_t[2] / 2.
            b1_y1, b1_y2 = box1_t[1] - box1_t[3] / 2., box1_t[1] + box1_t[3] / 2.
            b2_x1, b2_x2 = box2_t[0] - box2_t[2] / 2., box2_t[0] + box2_t[2] / 2.
            b2_y1, b2_y2 = box2_t[1] - box2_t[3] / 2., box2_t[1] + box2_t[3] / 2.
        elif self.coord_type == "ltrb":
            b1_x1, b1_y1 = 0. - box1_t[0], 0. - box1_t[1]
            b1_x2, b1_y2 = 0. + box1_t[2], 0. + box1_t[3]
            b2_x1, b2_y1 = 0. - box2_t[0], 0. - box2_t[1]
            b2_x2, b2_y2 = 0. + box2_t[2], 0. + box2_t[3]
        else:
            raise NotImplementedError("coord_type only support xyxy, xywh,ltrb")
        inter_area = (torch.min(b1_x2, b2_x2) - torch.max(b1_x1, b2_x1)).clamp(0) * \
                     (torch.min(b1_y2, b2_y2) - torch.max(b1_y1, b2_y1)).clamp(0)

        w1, h1 = b1_x2 - b1_x1, b1_y2 - b1_y1
        w2, h2 = b2_x2 - b2_x1, b2_y2 - b2_y1
        union_area = w1 * h1 + w2 * h2 - inter_area + self.eps
        iou = inter_area / union_area
        if self.iou_type == "iou":
            return iou

        cw = torch.max(b1_x2, b2_x2) - torch.min(b1_x1, b2_x1)
        ch = torch.max(b1_y2, b2_y2) - torch.min(b1_y1, b2_y1)
        if self.iou_type == "giou":
            c_area = cw * ch + self.eps
            giou = iou - (c_area - union_area) / c_area
            return giou

        diagonal_dis = cw ** 2 + ch ** 2 + self.eps
        center_dis = ((b2_x1 + b2_x2 - b1_x1 - b1_x2) ** 2 +
                      (b2_y1 + b2_y2 - b1_y1 - b1_y2) ** 2) / 4
        if self.iou_type == 'diou':
            diou = iou - center_dis / diagonal_dis
            return diou

        v = (4 / math.pi ** 2) * torch.pow(torch.atan(w2 / h2) - torch.atan(w1 / h1), 2)
        with torch.no_grad():
            alpha = v / ((1 + self.eps) - iou + v)

        if self.iou_type == "ciou":
            ciou = iou - (center_dis / diagonal_dis + v * alpha)
            return ciou

        raise NotImplementedError("iou_type only support iou,giou,diou,ciou")


class IOULoss(object):
    def __init__(self, iou_type="giou", coord_type="xyxy"):
        super(IOULoss, self).__init__()
        self.iou_type = iou_type
        self.box_similarity = BoxSimilarity(iou_type, coord_type)

    def __call__(self, predicts, targets):
        similarity = self.box_similarity(predicts, targets)
        if self.iou_type == "iou":
            iou_loss= -similarity.log()
        else:
            iou_loss= 1 - similarity
        return iou_loss



