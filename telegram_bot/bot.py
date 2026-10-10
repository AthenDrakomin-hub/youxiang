import os, sqlite3, random, string, logging, re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = "/root/TempMail-Panel/myserver_mail.db"
MAIL_DOMAIN = "yefeng.us.cc"
PAGE_SIZE = 5

# ============ 数据库层 ============
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def get_or_create_user(telegram_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM web_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    if user:
        conn.close()
        return dict(user)
    username = f"tg_{telegram_id}"
    token = ''.join(random.choices(string.ascii_lowercase + string.digits, k=64))
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO web_users (username, password, token, is_activated, created_at, telegram_id) VALUES (?, ?, ?, 1, ?, ?)",
        (username, password, token, now, telegram_id))
    conn.commit()
    user = conn.execute("SELECT * FROM web_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(user)

def create_mailbox(user_token):
    conn = get_db()
    while True:
        name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        email = f"{name}@{MAIL_DOMAIN}"
        if not conn.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
            break
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    conn.execute("INSERT INTO accounts (email, password, owner_token, remark) VALUES (?, ?, ?, ?)",
        (email, password, user_token, "Telegram Bot"))
    conn.commit()
    conn.close()
    return email

def list_mailboxes(user_token):
    conn = get_db()
    rows = conn.execute("SELECT email FROM accounts WHERE owner_token = ? ORDER BY rowid DESC", (user_token,)).fetchall()
    conn.close()
    return [r['email'] for r in rows]

def get_inbox_count(email):
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM emails WHERE to_address = ?", (email,)).fetchone()['c']
    conn.close()
    return count

def get_inbox_page(email, page=0):
    conn = get_db()
    offset = page * PAGE_SIZE
    rows = conn.execute("SELECT id, from_address, subject, datetime(timestamp) as time FROM emails WHERE to_address = ? ORDER BY id DESC LIMIT ? OFFSET ?", (email, PAGE_SIZE, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_email(mail_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (mail_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def delete_email(mail_id):
    conn = get_db()
    conn.execute("DELETE FROM emails WHERE id = ?", (mail_id,))
    conn.commit()
    conn.close()

def extract_verification_code(text):
    """从邮件正文中提取验证码"""
    patterns = [
        r'验证码[：:\s]*([A-Za-z0-9]{4,8})',
        r'code[：:\s]*([A-Za-z0-9]{4,8})',
        r'OTP[：:\s]*([A-Za-z0-9]{4,8})',
        r'(\d{4,6})',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None

# ============ UI 组件 ============
def main_menu_keyboard():
    """底部常驻菜单"""
    keyboard = [
        [KeyboardButton("📧 新建邮箱"), KeyboardButton("📋 我的邮箱")],
        [KeyboardButton("📥 收件箱"), KeyboardButton("❓ 帮助")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def new_mailbox_actions(email):
    """创建邮箱后的操作按钮"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 查看收件箱", callback_data=f"inbox:{email}:0", style="success")],
        [InlineKeyboardButton("🔄 再创建一个", callback_data="new", style="primary")],
        [InlineKeyboardButton("📋 复制邮箱地址", callback_data=f"copy:{email}", style="primary")],
    ])

def mailbox_list_keyboard(emails, page=0):
    """邮箱列表按钮"""
    buttons = []
    for email in emails:
        count = get_inbox_count(email)
        label = f"📧 {email}" + (f" ({count}封)" if count > 0 else "")
        buttons.append([InlineKeyboardButton(label, callback_data=f"inbox:{email}:0", style="primary")])
    return InlineKeyboardMarkup(buttons) if buttons else None

def inbox_keyboard(email, mails, page, total):
    """收件箱分页按钮"""
    buttons = []
    for m in mails:
        subject = m['subject'][:30] if m['subject'] else "(无主题)"
        buttons.append([InlineKeyboardButton(f"📨 {subject}", callback_data=f"read:{m['id']}:{email}:{page}", style="primary")])
    # 分页
    nav = []
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"inbox:{email}:{page-1}", style="primary"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"inbox:{email}:{page+1}", style="primary"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔄 刷新", callback_data=f"inbox:{email}:{page}", style="success")])
    return InlineKeyboardMarkup(buttons)

def email_detail_keyboard(mail_id, email, page):
    """邮件详情按钮"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 删除邮件", callback_data=f"del:{mail_id}:{email}:{page}", style="danger")],
        [InlineKeyboardButton("← 返回收件箱", callback_data=f"inbox:{email}:{page}", style="primary")],
    ])

# ============ 消息渲染 ============
def render_welcome():
    return (
        "👋 <b>欢迎使用金洋临时邮箱</b>\n\n"
        "快捷创建临时邮箱，自动接收验证码。\n\n"
        "📌 <b>使用方式：</b>\n"
        "• 点击底部菜单或发送命令\n"
        "• 创建邮箱后去注册账号\n"
        "• 验证码邮件自动推送到这里\n\n"
        f"🌐 邮箱后缀：<code>@{MAIL_DOMAIN}</code>"
    )

def render_new_mailbox(email):
    return (
        "✅ <b>新邮箱创建成功！</b>\n\n"
        f"📧 邮箱地址：\n<code>{email}</code>\n\n"
        "💡 用这个邮箱去注册账号，验证码会自动推送到这里。\n"
        "点击下方按钮快速操作："
    )

def render_mailbox_list(emails):
    if not emails:
        return "📭 还没有邮箱，点击「📧 新建邮箱」创建一个吧！"
    text = "📋 <b>我的邮箱列表</b>\n\n"
    for i, email in enumerate(emails, 1):
        count = get_inbox_count(email)
        text += f"{i}. <code>{email}</code>"
        if count > 0:
            text += f"  <b>({count}封未读)</b>"
        text += "\n"
    text += f"\n共 <b>{len(emails)}</b> 个邮箱，点击下方按钮查看收件箱："
    return text

def render_inbox(email, mails, page, total):
    if not mails:
        return f"📭 <code>{email}</code>\n\n收件箱为空，等待邮件中..."
    text = f"📥 <b>收件箱</b>\n<code>{email}</code>\n"
    text += f"第 {page+1} 页 / 共 {total} 封\n\n"
    for m in mails:
        subject = m['subject'] if m['subject'] else "(无主题)"
        text += f"{'─'*20}\n"
        text += f"📨 <b>{subject}</b>\n"
        text += f"👤 {m['from_address']}\n"
        text += f"🕐 {m['time']}\n"
        text += f"🔢 ID: {m['id']}\n"
    text += f"\n{'─'*20}\n点击邮件查看详情："
    return text

def render_email_detail(mail):
    code = extract_verification_code(mail.get('body', ''))
    text = "📧 <b>邮件详情</b>\n\n"
    text += f"👤 发件人：{mail['from_address']}\n"
    text += f"📥 收件人：<code>{mail['to_address']}</code>\n"
    text += f"📝 主题：<b>{mail['subject'] or '(无主题)'}</b>\n"
    text += f"🕐 时间：{mail['timestamp']}\n"
    if code:
        text += f"\n🔑 <b>检测到验证码：<code>{code}</code></b>\n"
    text += f"\n{'─'*20}\n"
    body = mail.get('body', '')[:3500]
    # 简单HTML转义
    body = body.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text += body
    if len(mail.get('body', '')) > 3500:
        text += "\n\n（内容过长已截断）"
    return text

# ============ 命令处理器 ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_or_create_user(update.effective_user.id)
    await update.message.reply_photo(photo="https://aka.doubaocdn.com/s/ZXUo20bjJR", caption=render_welcome(), parse_mode="HTML", reply_markup=main_menu_keyboard())

async def new_mailbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_or_create_user(update.effective_user.id)
    email = create_mailbox(user['token'])
    await update.message.reply_text(render_new_mailbox(email), parse_mode="HTML", reply_markup=new_mailbox_actions(email))

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_or_create_user(update.effective_user.id)
    emails = list_mailboxes(user['token'])
    await update.message.reply_text(render_mailbox_list(emails), parse_mode="HTML", reply_markup=mailbox_list_keyboard(emails))

async def inbox_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_or_create_user(update.effective_user.id)
    emails = list_mailboxes(user['token'])
    if not emails:
        await update.message.reply_text("📭 还没有邮箱，点击「📧 新建邮箱」创建一个吧！", reply_markup=main_menu_keyboard())
        return
    email = emails[0]
    total = get_inbox_count(email)
    mails = get_inbox_page(email, 0)
    await update.message.reply_text(render_inbox(email, mails, 0, total), parse_mode="HTML", reply_markup=inbox_keyboard(email, mails, 0, total))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>帮助说明</b>\n\n"
        "📧 <b>新建邮箱</b> - 创建随机临时邮箱\n"
        "📋 <b>我的邮箱</b> - 查看所有邮箱\n"
        "📥 <b>收件箱</b> - 查看最新邮箱的邮件\n\n"
        "💡 <b>使用技巧：</b>\n"
        "• 新邮件到达会自动推送\n"
        "• 推送消息点击「查看完整内容」看全文\n"
        "• 验证码会自动识别并高亮\n"
        "• 邮箱地址点击可复制\n\n"
        f"🌐 邮箱后缀：<code>@{MAIL_DOMAIN}</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())

async def menu_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理底部菜单按钮点击"""
    text = update.message.text
    if "新建邮箱" in text:
        await new_mailbox(update, context)
    elif "我的邮箱" in text:
        await list_cmd(update, context)
    elif "收件箱" in text:
        await inbox_cmd(update, context)
    elif "帮助" in text:
        await help_cmd(update, context)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "new":
        user = get_or_create_user(update.effective_user.id)
        email = create_mailbox(user['token'])
        await query.edit_message_text(render_new_mailbox(email), parse_mode="HTML", reply_markup=new_mailbox_actions(email))

    elif data.startswith("copy:"):
        email = data[5:]
        await query.answer(text=f"已复制：{email}", show_alert=True)

    elif data.startswith("inbox:"):
        parts = data.split(":")
        email = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        total = get_inbox_count(email)
        mails = get_inbox_page(email, page)
        await query.edit_message_text(render_inbox(email, mails, page, total), parse_mode="HTML", reply_markup=inbox_keyboard(email, mails, page, total))

    elif data.startswith("read:"):
        parts = data.split(":")
        mail_id = int(parts[1])
        email = parts[2]
        page = int(parts[3])
        mail = get_email(mail_id)
        if mail:
            await query.edit_message_text(render_email_detail(mail), parse_mode="HTML", reply_markup=email_detail_keyboard(mail_id, email, page))

    elif data.startswith("del:"):
        parts = data.split(":")
        mail_id = int(parts[1])
        email = parts[2]
        page = int(parts[3])
        delete_email(mail_id)
        total = get_inbox_count(email)
        mails = get_inbox_page(email, page)
        await query.edit_message_text(f"🗑 邮件已删除\n\n" + render_inbox(email, mails, page, total), parse_mode="HTML", reply_markup=inbox_keyboard(email, mails, page, total))

# ============ 新邮件推送 ============
last_pushed = {}

async def check_new_mails(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    users = conn.execute("SELECT telegram_id, token FROM web_users WHERE telegram_id IS NOT NULL").fetchall()
    for user in users:
        tg_id, token = user['telegram_id'], user['token']
        emails = [r['email'] for r in conn.execute("SELECT email FROM accounts WHERE owner_token = ?", (token,)).fetchall()]
        if not emails:
            continue
        last_id = last_pushed.get(tg_id, 0)
        ph = ','.join(['?'] * len(emails))
        new_mails = conn.execute(f"SELECT * FROM emails WHERE id > ? AND to_address IN ({ph}) ORDER BY id ASC", (last_id, *emails)).fetchall()
        for mail in new_mails:
            code = extract_verification_code(mail.get('body', ''))
            text = "📬 <b>新邮件到达！</b>\n\n"
            text += f"📧 <code>{mail['to_address']}</code>\n"
            text += f"👤 {mail['from_address']}\n"
            text += f"📝 <b>{mail['subject'] or '(无主题)'}</b>\n"
            if code:
                text += f"\n🔑 <b>验证码：<code>{code}</code></b>\n"
            text += f"\n{mail['body'][:300]}".replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            if len(mail['body']) > 300:
                text += "..."
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📖 查看完整内容", callback_data=f"read:{mail['id']}:{mail['to_address']}:0", style="success")
            ]])
            try:
                await context.bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML", reply_markup=kb)
            except Exception as e:
                logging.error(f"推送失败: {e}")
            last_pushed[tg_id] = mail['id']
    conn.close()

# ============ 主入口 ============
def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN environment variable not set")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_mailbox))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("inbox", inbox_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_message))
    app.job_queue.run_repeating(check_new_mails, interval=5, first=1)
    logging.info("Bot 启动成功 (v2.0 优化版)")
    app.run_polling()

if __name__ == "__main__":
    main()