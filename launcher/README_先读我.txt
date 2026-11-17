FlashRel Rig 2 — USB 闪存盘寿命测试（Run 2：20 个 U 盘、两个 10 口拓展坞、5 天）
=====================================================================

这个文件夹自带 Python，测试电脑上什么都不用装。详细步骤见 docs\operator_guide_zh.md。

一、放到测试电脑上
  1. 如果是从网上（Google Drive / 邮件）下载的 ZIP：先右键 ZIP → 属性 → 勾选“解除锁定/Unblock” → 确定，再解压。
  2. 把整个 FlashRel-Rig 文件夹放到电脑自己的硬盘上，推荐 C:\FlashRel-Rig
     （不要放在 U 盘、OneDrive、Google Drive、Dropbox 同步文件夹里）。

二、连接拓展坞（Sabrent HB-BU10 ×2）
  - 两个拓展坞都接上自带的 12V 电源适配器（插同一个插线板），每个口的小开关全部打开，测试期间不要动。
  - 每个拓展坞的线直接插在电脑【背面】的 USB 口上（不要用前面板、不要延长线、不要把一个拓展坞插到另一个上）。
    两个拓展坞插在两个分开的口上（最好是不同的 USB 控制器）。
  - 电脑本身只需要 2 个空闲的 USB-A 口；键盘鼠标插在别的口。测试盘一个都不插在电脑本身上。
  - 每个拓展坞只有一条 USB 2.0 通道（约 40 MB/s），10 个盘共享，软件会记录每个坞的负载。
  - 给每个 U 盘贴上编号标签（A16-03…A16-12、A8-06…A8-15、S8-01、S8-02），给拓展坞的口贴 1–10 号（1 号 = 离线缆最近的口）。

三、开始
  1. 双击 “Start FlashRel.bat”，Windows 问管理员权限时点“是”。浏览器会打开仪表盘（Dashboard）。
  2. 在 Setup 页按 1–5 步走：
     ① 修 PC 设置（红/黄的项点按钮修，测试后可一键还原）
     ② 按图把盘对应到拓展坞的口
     ③ 先插备用盘 A8-09，登记(enroll)后点 Self-test；再登记 A8-15；然后两个备用盘拔下来留着
     ④ 按提示一个一个插测试盘、点 “Enroll as …”（会格式化该盘！）
     ⑤ 全部 20 个登记好后点 “Start the 120-hour test”
  3. 之后不用管它，120 小时后自动停止。可以关掉浏览器，测试照样跑；再双击 Start FlashRel.bat 就能重新打开仪表盘。

四、测试期间（每天看一两次就行）
  - 不要拔插任何 U 盘，不要动拓展坞开关，不要让电脑睡眠/重启。
  - Windows 弹出“扫描并修复 / 格式化”之类的提示：一律点取消（损坏的盘是证据）。
  - 可以在 Data & control 页记录室温/拓展坞温度（红外测温枪或温度计，每天一次即可）和备注。
  - 坏掉的盘会变红/粉（FAILED / ATTENTION）：让它留在原位，测试结束后再处理。

五、结束后
  1. 按 Post-test 页的步骤做测试后检查（re-check、port check），每一步都有按钮。
  2. 在 Data & control 页点 “Export ZIP”，把 exports\ 里的 ZIP（或者整个 data 文件夹）发回来做分析。
  3. Setup 页 “Undo host changes” 可把电脑设置还原。

紧急情况：仪表盘打不开又需要停 → 双击 “Stop FlashRel.bat”。之后再双击 Start 可以继续测试。

---------------------------------------------------------------------
English summary: copy the folder to C:\FlashRel-Rig (unblock the ZIP first), plug each
hub (with its 12 V adapter) straight into a rear USB port of the PC, double-click
"Start FlashRel.bat" and accept the administrator prompt, then follow the Setup tab of
the dashboard (host checks -> spare + self-test -> enroll the 20 drives one at a time ->
start). The test stops by itself after 120 hours. Then follow the Post-test tab and
press "Export ZIP"; send back the ZIP from exports\ or the whole data\ folder.
