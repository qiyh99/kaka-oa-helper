# 咔咔 OA 助手 · by [qiyh99](https://github.com/qiyh99)

一个本地小工具：自动统计**近三个月加班 / 调休**、算出**剩余可调休时长**、提醒**七天内即将作废**的调休，并顺带查看**个人绩效**。

全程**零配置**——不用扫码、不用输账号密码、不用手动找 token。双击即用。

## ✨ 功能

- **调休结算**：近三个月加班、调休一目了然，按「加班 1:1 折算调休、满 3 个月未用作废」自动结算出剩余可调休。
- **到期提醒**：七天内即将作废的调休单独高亮提醒（最早到期先用估算）。
- **年假结算**：按工龄算法定年假（满1年5天 / 满10年10天 / 满20年15天），查近两年使用情况，算出剩余年假（上一年未休可结转一年）。工龄默认按绩效推算，可手填覆盖。
- **个人绩效**：月度等级 / 最终分 / 自评 / 初评 / 终评。
- **工资估算**：手填工资+补贴，自动按「基本80%+绩效20%×绩效分」算应发，减去成都五险（养老8%/医疗2%/失业0.4%）和公积金（工资一半×8%），得出税前到手。绩效分默认取最新月绩效。
- **全自动登录态**：自动从本机微信里读取登录态，无需任何手动操作。

## 🚀 使用

### 方式一：下载 exe（推荐，免装环境）
1. 到 [最新 Release](https://github.com/qiyh99/kaka-oa-helper/releases/latest) 下载 `kaka_oa.exe`（版本变更见 [CHANGELOG](CHANGELOG.md)）。
2. 先用**电脑版微信**打开过一次绩效 / OA 页面（让微信存下登录态）。
3. 双击 `kaka_oa.exe` —— 浏览器自动弹出，直接显示你自己的数据。
4. 用完点页面右上角「**关闭程序**」即可退出。

> 首次双击可能被 Windows SmartScreen / 杀软拦一下（未签名的自打包程序常见），点「更多信息 → 仍要运行」即可。

### 方式二：源码运行
```bash
pip install -r requirements.txt
python kaka_tiaoxiu.py
```

## 🧩 原理

绩效 / OA 的 H5 在微信里走的是**微信网页授权**，登录态是一个叫 `tokenId7` 的 cookie，存在 PC 微信的内置浏览器里。本工具：

1. 自动发现并从本机微信的 Chromium cookie 库里解出 `tokenId7`（DPAPI + AES-GCM，和 Chrome 同套加密）；
2. 用它直接调 `kk.xwtec.net` 的 OA / 绩效接口，本地算好后在网页里展示。

所有数据只在你本机处理，不上传任何地方。

## 👥 给同事用

每人在**自己电脑**上运行（读各自微信），即各自全自动、互不影响。

## ❓ 读不到登录态 / 检测不到目录

1. 先确认用**电脑版微信**打开过一次绩效 / OA 页（让微信存下登录态）。
2. 页面会出现「微信目录」输入框，填进去再点「从该目录读取」。默认目录直接复制 `%APPDATA%\Tencent` 即可。常见完整位置：
   - 新版微信：`C:\Users\用户名\AppData\Roaming\Tencent\xwechat\radium\web\profiles`
   - 旧版微信：`C:\Users\用户名\AppData\Roaming\Tencent\WeChat\xweb`
   - 也可以直接填那个名为 `Cookies` 的文件路径。
3. 实在不行：运行 `kaka_get_token.py`（可带目录参数 `python kaka_get_token.py "%APPDATA%\Tencent"`）拿到 token，粘到网页最下面的输入框。

## 🔨 自行打包

```bash
pip install pyinstaller
python -m PyInstaller --onefile --noconsole --icon favicon.ico --noconfirm --clean --name kaka_oa kaka_tiaoxiu.py
```
产物在 `dist/kaka_oa.exe`。

## 📦 依赖

- Python 3.8+
- `requests`、`flask`、`cryptography`

## ⚠️ 说明

仅供个人查询自己的 OA 数据使用，请勿用于他人账号。本项目与新讯 / 咔咔官方无关。

---
Made with ❤️ by [qiyh99](https://github.com/qiyh99)
