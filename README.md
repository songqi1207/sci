# ScienceDirect 最新一期论文抓取

从 ScienceDirect 期刊的 `latest` 页面发现最新一期论文，并逐篇抓取公开的题目、摘要、作者、DOI、PII、文献类型及访问状态，最终导出 JSON、CSV 和 Markdown。

默认示例期刊为 [Machine Learning with Applications](https://www.sciencedirect.com/journal/machine-learning-with-applications/latest)。

## 实现要点

- 使用 Python Playwright 驱动真实 Chrome，处理 JavaScript 动态页面。
- 复用期刊页浏览器会话和来源信息访问论文页；不使用无状态请求批量冲击论文 URL。
- 通过显式等待、低频随机间隔和逐篇容错提高稳定性。
- 检测访问限制并明确报错，不绕过验证码或登录限制。
- 论文页受限时可按 DOI 从 OpenAlex、Crossref 补全摘要，并在结果中标记 `abstract_source`。
- 全部结果带原始 ScienceDirect 链接，支持 JSON、UTF-8 CSV 和 Markdown。

## 环境

- Python 3.9+
- Google Chrome
- Windows、macOS 或 Linux

## 安装

Windows PowerShell（示例使用 Python 3.13）：

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

程序直接调用本机 Google Chrome，不需要单独下载浏览器或 ChromeDriver。

## 运行

抓取默认期刊最新一期的前 10 篇：

```bash
python scraper.py
```

抓取当前最新一期的全部论文：

```bash
python scraper.py --limit 0
```

先抓取 3 篇做快速验证：

```bash
python scraper.py --limit 3
```

若当前网络已被论文页限流，可跳过论文页，仅从 ScienceDirect 抓题录并按 DOI 补全摘要：

```bash
python scraper.py --metadata-fallback-only --limit 10
```

更换期刊：

```bash
python scraper.py --journal-url "https://www.sciencedirect.com/journal/journal-name/latest"
```

默认会显示浏览器窗口，因为 ScienceDirect 可能拒绝无头浏览器。若网络允许，可尝试 `--headless`。遇到验证码时请停止程序并稍后重试，不要自动绕过。

## 输出

运行完成后生成：

- `output/articles.json`：完整结构化结果与失败记录
- `output/articles.csv`：适合 Excel、Pandas 或数据库导入
- `output/articles.md`：适合直接浏览和分享

`output/sample_articles.*` 是一次真实运行的公开元数据样例。每条数据的 `abstract_source` 会明确记录摘要来自 ScienceDirect、OpenAlex 或 Crossref。

当前样例从 Volume 26（December 2026）前 10 条题录中获得 8 篇完整摘要；另外 2 篇在公开元数据源中没有摘要，已记录在 JSON 的 `failures` 字段中。

## 测试

解析逻辑不依赖网站，可直接运行：

```bash
python -m unittest discover -s tests -v
```

## 合规说明

本项目仅演示低频获取公开题录与摘要，不下载付费全文、不处理登录内容、不规避验证码。实际使用前请确认目标页面的服务条款、robots 规则及所在地区适用要求。
