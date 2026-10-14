# Atelier Zero 拼贴素材

此目录用于存放 16 张拼贴素材，用于空状态、背景、占位图。

## 命名约定

```
collage_01.png ~ collage_16.png
```

## 风格建议

- 纸感纹理、轻微褶皱、胶带、裁切边缘
- 高对比黑白摄影 + Études 蓝 (#1F3AFF) 点缀
- 档案标签、期号、索引编号等编辑元素
- 避免 emoji、卡通、圆角、渐变、玻璃拟态

## 自动生成占位图

运行以下命令会在目录为空时自动生成 16 张 1×1 占位 PNG：

```python
from ui.theme import ensure_collage_assets
ensure_collage_assets()
```

请在发布前替换为真实设计素材。
