# Hermes Agent 版本稳定性每日监控

每日 06:00 自动采集 Hermes Agent 各维度量化指标，持续积累数据并计算综合评分。

## 目录结构

```
hermes-stability-metrics/
├── README.md
├── metrics/
│   ├── SCORING.md          # 评分体系说明
│   ├── YYYY-MM-DD.json     # 每日指标快照
│   └── summary.json        # 历史趋势汇总
├── scripts/
│   ├── collect.py          # 指标采集脚本
│   └── score.py            # 评分计算
└── reports/
    └── YYYY-MM-DD.md       # 每日评估报告
```

## 评分体系（0-100 分）

详见 [SCORING.md](./metrics/SCORING.md)
