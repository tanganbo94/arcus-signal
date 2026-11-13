Arcus 单向合约：本地威科夫扫描 + 确认下单
======================================

在你自己的电脑上运行，不经过机器人下单。行情用免费数据（CoinGecko 市值榜、币安公开接口、主流媒体 RSS），不需要 CoinGlass 付费 API。

和桌面上的 arcus-entropy-arb（价差对冲）是两套东西，请分开文件夹，不要混用同一份正在跑对冲的窗口去自动开单向仓。
若 Arcus 账户里已经有对冲仓（例如 NEAR），先不要用本脚本开新的单向仓。

一、准备
1. 把本文件夹放到要运行的电脑上。
2. 复制 API.txt.example 为 API.txt，只填 Arcus 三项：
   ARCUS_SIGNING_KEY
   ARCUS_ACCOUNT_INDEX
   ARCUS_WALLET_ADDRESS
   不需要 Entropy / Hyperliquid 密钥。不要把 API.txt 上传或发到聊天。
3. 若访问币安或 CoinGecko 超时，在 API.txt 加上：
   PROXY=http://127.0.0.1:10808
   （端口按你电脑上的代理修改）
4. 安装依赖：
   py -m pip install -r requirements.txt

二、扫描（不下单）
   双击 start_scan.bat
   或： py scan.py
   或： py scan.py --coin BTC
   加上 --status 会同时查询 Arcus 账户持仓。

只提示新的、已经确认的可操作结构。弹簧或上冲回落本身只算观察，要等到 SOS / 离开区间 / 最后点支撑，或空头的弱势信号 / 破冰 / 弹簧失败，才给信号。只用已收盘的日线和 4 小时线，差一点的形态和未收盘K线不算。旧结构不会反复提醒。
新闻只作背景，不会单凭标题给开仓信号。止损按结构失效位提示，不是价差止盈止损。

三、下单（默认先干跑）
   py trade_one.py --status
   py trade_one.py --dry-run --side long --coin BTC --notional 2000
   确认无误后：
   py trade_one.py --live --i-confirm --side long --coin BTC --notional 2000

没有同时写 --live --i-confirm，不会发送订单。

四、给另一台电脑用
把整个文件夹拷过去，在那台电脑单独建立 API.txt（填那台要用的 Arcus 账户）。不要复制正在跑对冲的那份密钥，除非你确定要用同一个账户。

五、本地自动扫描并开平仓（推荐日常用法）
   双击 start_watch.bat，窗口保持打开。脚本会：
   - 扫描 Arcus 全部在线永续（加密、美股、指数、商品）
   - 只在出现新的、已确认的威科夫结构（不只是弹簧或上冲），且盈亏比至少 1.5 倍时自动开仓
   - 美股 / 指数 / 商品只在常规交易时段开新仓；已有仓位会持续管理
   - 止损：弹簧低点下方（多）或上冲高点上方（空）
   - 浮盈达到 1 倍风险后，止损移到成本附近，并跟踪 4 小时结构
   - 止盈：20 日区间对侧；触及后自动平仓
   - 开仓后会在交易所同时挂上仓位级止盈和止损；本地盯盘跟踪止损变化时，会撤销旧单再改挂
   默认每笔名义金额 50 U、最多同时 5 笔（可在 API.txt 改 NOTIONAL 和 MAX_POSITIONS）。加密品种市值低于 1 亿美元（MIN_MARKET_CAP）不开新仓，查不到市值也不开；已有仓位照常管理。
   同一品种不加仓。要停止时在窗口按 Ctrl+C。
   只预览不下单：双击 start_watch_dry.bat，或 py watch.py --dry-run
