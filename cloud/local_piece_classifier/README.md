# 象棋棋子字符分类器（PaddleClas / PP-LCNet）

此目录训练的是 14 类单棋子图像分类模型。原始样本已经是 `160x160` 的单棋子裁剪图，
因此使用 PP-LCNet 分类网络；PP-YOLO/PP-YOLOE+ 是目标检测网络，需要棋盘原图和边界框标注，
不适合当前按类别文件夹整理的数据。

## 数据

默认读取：

`../../training_dataset/piece_character_dataset/images/needs_review/<class>/*.jpg`

`prepare_dataset.py` 会校验图片、检查重复文件，并按原始棋盘帧分组切分训练集和验证集，
防止同一棋盘截图中的棋子同时进入两边造成指标虚高。生成结果位于：

`../../training_dataset/piece_character_dataset/paddleclas/`

## 使用

在 PowerShell 中执行：

```powershell
cd cloud/local_piece_classifier
.\setup_env.ps1
.\train.ps1
```

`train.ps1` 默认后台运行。查看日志：

```powershell
Get-Content .\logs\train.log -Wait
```

前台运行或从最近检查点恢复：

```powershell
.\train.ps1 -Foreground
.\train.ps1 -Resume
```

最佳参数默认保存在 `output/xiangqi_pplcnet_x1_0/best_model.pdparams`。
训练完成后导出推理模型：

```powershell
.\export.ps1
```

对棋子裁剪图运行已导出的静态推理模型：

```powershell
.\.venv\Scripts\python.exe .\predict.py path\to\piece.jpg
```

在与训练数据来源帧不重叠的 `auto_labeled` 图片上测试：

```powershell
.\.venv\Scripts\python.exe .\evaluate.py --cpu
```

结果保存在 `output/test_evaluation/test_results.json` 和 `confusion_matrix.csv`。

将 `auto_labeled` 目录中的全部图片纳入测试（包括来源帧重叠样本）：

```powershell
.\.venv\Scripts\python.exe .\evaluate.py --cpu --include-overlapping-frames `
    --output-dir .\output\test_evaluation_all_auto_labeled
```
