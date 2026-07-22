"""本地身份信息示例。

需要登录态时，把本文件复制为 config.py，再填写值。
config.py 已被 .gitignore 排除，请勿把 Cookie 提交到 GitHub。
"""

# 浏览器开发者工具 -> 网络 -> 评论请求 -> 请求标头 -> Cookie
COOKIE = ""

# 一般可以留空，程序会自动从 COOKIE 的 __csrf 字段提取。
CSRF_TOKEN = ""
