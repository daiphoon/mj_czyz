# DOCX 模板提取清单

- 参考文件：`/Users/lizhi/Documents/民建工作/社情民意/完善北京市新就业形态劳动者平台算法判罚与申诉纠错机制的建议.docx`
- SHA-256：`1a9e91ddf24b22c5380ac89e6071a29e39de9ec9e373c5dc0db9929a395cc94f`
- 源文件：14,007 字节；2 页；1 个节；19 个正文段落；无表格、图片、页眉或页脚。
- 源节未显式声明纸张、边距、页眉和页脚距离，依赖应用默认值；为保证跨机一致性，项目模板明确固化为 A4，页边距上/下 25.4 mm、左/右 31.8 mm。
- 源 `Heading 1`：20 pt，主题标题字体，主题色 `0F4761`，段前 18 pt、段后 4 pt，保持与下段同页。
- 源 `Heading 2`：16 pt，主题标题字体，主题色 `0F4761`，段前 8 pt、段后 4 pt，保持与下段同页。
- 源正文：12 pt，主题正文字体；`Body Text` 段前/后 9 pt；首行通过两个全角空格表达。
- 源署名：`First Paragraph`，使用 25 个全角空格模拟右对齐。
- 项目模板的有意固化：中文字体使用等线（OOXML 字体名 `DengXian`），黑色标题，正文 1.5 倍行距，标题居中，署名真正右对齐，正文使用 2 字符首行缩进。标题、一级标题和正文仍分别保持 20、16、12 pt，不因字体调整改变字号。
- 导出器同时在样式和每个可见文本 run 上写入 `ascii`、`hAnsi`、`eastAsia`、`cs=DengXian`，避免 LibreOffice 或跨应用导出时把粗体标题替换为其他中文字体。
- 固定槽位：标题、署名、一、现状、二、问题和分析、三、政策建议。
- 保留项：三级结构、标题层级、默认署名、正文无图表/脚注/参考文献。

## macOS 渲染说明

本机等线字体由 Microsoft Word 以私有字体形式放在 `/Applications/Microsoft Word.app/Contents/Resources/DFonts/`。直接运行隔离版 LibreOffice 时可能显示方框。逐页QA时应把 `Deng.ttf`、`Dengb.ttf`、`Dengl.ttf` 临时复制到本次 LibreOffice profile 的 `Library/Fonts/`，并将 `FONTCONFIG_FILE` 指向所用 LibreOffice 包内的 `Resources/fontconfig/fonts.conf`。字体只放临时目录，不修改用户字体库。验收时除检查页面PNG，还应使用 `pdffonts` 确认正文和标题分别嵌入 `DengXian-Regular`、`DengXian-Bold`。
