
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, ChatMemberHandler, MessageHandler, filters
from src.config import BOT_TOKEN
from src.handlers.commands import start, import_admins, whitelist_user, handle_chat_shared
from src.handlers.member_join import check_impersonation
from src.db import init_db

# Setup logging - set to WARNING to reduce noise
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # Keep our logger at INFO

# Suppress httpx polling logs
logging.getLogger("httpx").setLevel(logging.WARNING)

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in .env file.")
        exit(1)

    # Initialize Database
    init_db()

    # CRITICAL: Enable chat_member updates to receive join/leave events
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    
    # Enable chat_member updates
    application.bot_data['chat_member_updates'] = True
    
    start_handler = CommandHandler('start', start)
    import_handler = CommandHandler('import_admins', import_admins)
    whitelist_handler = CommandHandler('whitelist', whitelist_user)
    
    # Handle chat shared from private setup flow
    chat_shared_handler = MessageHandler(filters.StatusUpdate.CHAT_SHARED, handle_chat_shared)
    
    # Handle new members
    member_handler = ChatMemberHandler(check_impersonation, ChatMemberHandler.CHAT_MEMBER)

    application.add_handler(start_handler)
    application.add_handler(import_handler)
    application.add_handler(whitelist_handler)
    application.add_handler(chat_shared_handler)
    application.add_handler(member_handler)
    
    logger.info("Bot is polling...")
    
    # Send startup notification to log channel
    async def post_init(app):
        from src.config import LOG_CHANNEL_ID
        if LOG_CHANNEL_ID:
            try:
                await app.bot.send_message(
                    chat_id=LOG_CHANNEL_ID,
                    text="🟢 **Anti-Impersonator Bot Started**\n\nThe bot is now online and monitoring for impersonators.",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.warning(f"Could not send startup message to log channel: {e}")
    
    application.post_init = post_init
    
    # CRITICAL: Explicitly request chat_member updates
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
