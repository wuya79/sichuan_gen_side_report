四川燃煤电厂发电侧交易日报系统

## 目录结构
- gen_txt.py — 从售电侧txt提取数据生成发电侧txt
- generate_pdf.py — Kimi+matplotlib+weasyprint生成PDF
- assets/report.css — PDF样式

## 定时任务
- 每日09:50: python3 gen_txt.py && python3 generate_pdf.py
## 依赖
- Kimi API (moonshot-v1-128k)
- raydon_api (四川电力数据API封装)
- weasyprint / matplotlib
