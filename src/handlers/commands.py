
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, KeyboardButtonRequestChat, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus, ChatType
from src.db import get_connection
from src.utils.image import compute_pfp_hash_bytes
from src.utils.detector import check_username_similarity, check_name_similarity, check_homoglyph_danger
from src.utils.image import check_pfp_similarity
from src.config import NAME_SIMILARITY_THRESHOLD, PFP_HASH_THRESHOLD
import logging

logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        # Provide a button to pick a group
        keyboard = [
            [KeyboardButton(
                "Select Group to Setup", 
                request_chat=KeyboardButtonRequestChat(
                    request_id=1, 
                    chat_is_channel=False,
                    bot_is_member=True
                )
            )]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            "👋 Welcome! To setup the Anti-Impersonator bot, please select a group you manage:",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "Anti-Impersonation Bot Active.\n"
            "Commands:\n"
            "/import_admins - Whitelist all admins in this chat\n"
            "/whitelist <reply> - Whitelist a specific user"
        )

async def handle_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the shared chat from the private setup flow."""
    shared_chat = update.message.chat_shared
    chat_id = shared_chat.chat_id
    
    await update.message.reply_text(f"Refreshing whitelist for group ID: {chat_id}...", reply_markup=ReplyKeyboardRemove())
    
    # Run the import logic
    success, message = await import_admins_logic(chat_id, update.effective_user.id, context)
    await update.message.reply_text(message)

async def import_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    
    # Check if user is admin
    member = await chat.get_member(user.id)
    if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
        await update.message.reply_text("Only admins can use this command.")
        return

    success, message = await import_admins_logic(chat.id, user.id, context)
    await update.message.reply_text(message)

async def import_admins_logic(chat_id: int, requester_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Core logic to import admins into the whitelist."""
    try:
        chat = await context.bot.get_chat(chat_id)
        admins = await chat.get_administrators()
    except Exception as e:
        logger.error(f"Failed to get admins for {chat_id}: {e}")
        return False, f"Error: Could not access the group. Make sure the bot is an admin in that group. ({e})"

    count = 0
    conn = get_connection()
    if not conn:
        return False, "Database connection failed."

    try:
        with conn.cursor() as cur:
            for admin in admins:
                user = admin.user
                if user.is_bot:
                    continue
                
                pfp_hash = None
                try:
                    photos = await user.get_profile_photos(limit=1)
                    if photos.total_count > 0:
                        photo_file = await photos.photos[0][-1].get_file()
                        file_content = await photo_file.download_as_bytearray()
                        pfp_hash = compute_pfp_hash_bytes(bytes(file_content))
                except Exception as e:
                    logger.warning(f"Could not get PFP for {user.id}: {e}")

                cur.execute("""
                    INSERT INTO whitelisted_users (user_id, username, first_name, last_name, pfp_hash, whitelisted_by, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        pfp_hash = EXCLUDED.pfp_hash,
                        updated_at = NOW();
                """, (user.id, user.username, user.first_name, user.last_name, pfp_hash, requester_id))
                count += 1
            
            conn.commit()
        return True, f"Successfully imported/updated {count} admins into the whitelist for {chat.title or chat_id}."
        
    except Exception as e:
        logger.error(f"Error in import_admins_logic: {e}")
        conn.rollback()
        return False, "An error occurred during DB operation."
    finally:
        conn.close()

async def whitelist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user message to whitelist them.")
        return

    target_user = update.message.reply_to_message.from_user
    conn = get_connection()
    
    try:
        with conn.cursor() as cur:
            pfp_hash = None
            try:
                photos = await target_user.get_profile_photos(limit=1)
                if photos.total_count > 0:
                    f = await photos.photos[0][-1].get_file()
                    file_content = await f.download_as_bytearray()
                    pfp_hash = compute_pfp_hash_bytes(bytes(file_content))
            except Exception as e:
                logger.warning(f"Could not get PFP for {target_user.id}: {e}")

            cur.execute("""
                INSERT INTO whitelisted_users (user_id, username, first_name, last_name, pfp_hash, whitelisted_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING;
            """, (target_user.id, target_user.username, target_user.first_name, target_user.last_name, pfp_hash, update.effective_user.id))
            conn.commit()
            
        await update.message.reply_text(f"User {target_user.full_name} has been whitelisted.")
    except Exception as e:
        logger.error(f"Error whitelisting user: {e}")
        await update.message.reply_text("Failed to whitelist user.")
    finally:
        if conn: conn.close()

