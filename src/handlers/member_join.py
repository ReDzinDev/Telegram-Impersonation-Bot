
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus
from src.db import get_connection
from src.utils.detector import check_username_similarity, check_name_similarity, check_homoglyph_danger
from src.utils.image import compute_pfp_hash_bytes, check_pfp_similarity
from src.config import NAME_SIMILARITY_THRESHOLD, PFP_HASH_THRESHOLD, LOG_CHANNEL_ID
import logging

logger = logging.getLogger(__name__)

async def check_impersonation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    new_member = result.new_chat_member
    
    # Trigger ONLY on new members joining from a 'left' or 'banned' state
    if result.old_chat_member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        return 
    
    if new_member.status not in [ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED]:
        return 

    user = new_member.user
    if user.is_bot:
        return
        
    logger.info(f"Checking new member {user.full_name} ({user.id}) for impersonation.")
    
    conn = get_connection()
    if not conn:
        return

    try:
        with conn.cursor() as cur:
            # Check if user is already whitelisted
            cur.execute("SELECT 1 FROM whitelisted_users WHERE user_id = %s", (user.id,))
            if cur.fetchone():
                logger.info(f"User {user.id} is whitelisted.")
                return 

            # Fetch all whitelisted users
            cur.execute("SELECT user_id, username, first_name, last_name, pfp_hash FROM whitelisted_users")
            whitelisted = cur.fetchall()
            
            # Prepare lists
            # whitelisted is a list of dicts because we used RowFactory=dict_row
            usernames = [w['username'] for w in whitelisted if w['username']]
            names = [f"{w['first_name']} {w['last_name'] or ''}".strip() for w in whitelisted]
            pfp_hashes = [w['pfp_hash'] for w in whitelisted if w['pfp_hash']]
            
            logger.info(f"Whitelist data: {len(usernames)} usernames, {len(names)} names, {len(pfp_hashes)} pfp hashes")
            
            # 1. Check Username
            if user.username:
                logger.info(f"Checking username: {user.username} against {len(usernames)} whitelisted usernames")
                match, matched_val, score = check_username_similarity(user.username, usernames, NAME_SIMILARITY_THRESHOLD)
                logger.info(f"Username check result: match={match}, score={score}, threshold={NAME_SIMILARITY_THRESHOLD}")
                if match:
                    await ban_and_log(update, context, user, "username", matched_val, score, whitelisted, conn)
                    return

            # 1.5 Check Homoglyphs
            if user.username and check_homoglyph_danger(user.username):
                 logger.info(f"Homoglyph detected in username: {user.username}")
                 await ban_and_log(update, context, user, "homoglyph_username", user.username, 100, [], conn)
                 return
            
            full_name = f"{user.first_name} {user.last_name or ''}".strip()
            logger.info(f"Checking name: '{full_name}' against {len(names)} whitelisted names")
            
            if check_homoglyph_danger(full_name):
                 logger.info(f"Homoglyph detected in name: {full_name}")
                 await ban_and_log(update, context, user, "homoglyph_name", full_name, 100, [], conn)
                 return

            # 2. Check Display Name
            # full_name already computed
            match, matched_val, score = check_name_similarity(full_name, names, NAME_SIMILARITY_THRESHOLD)
            logger.info(f"Name check result: match={match}, score={score}, matched='{matched_val}', threshold={NAME_SIMILARITY_THRESHOLD}")
            if match:
                 await ban_and_log(update, context, user, "name", matched_val, score, whitelisted, conn)
                 return

            # 3. Check PFP
            photos = await user.get_profile_photos(limit=1)
            logger.info(f"User has {photos.total_count} profile photos")
            if photos.total_count > 0:
                photo_file = await photos.photos[0][-1].get_file()
                file_content = await photo_file.download_as_bytearray()
                target_hash = compute_pfp_hash_bytes(bytes(file_content))
                
                logger.info(f"Computed PFP hash: {target_hash}")
                
                if target_hash:
                    # check_pfp_similarity returns (match, matched_hash, distance)
                    # Note: score here is distance (lower is better check logic in utils)
                    match, matched_val, dist = check_pfp_similarity(target_hash, pfp_hashes, PFP_HASH_THRESHOLD)
                    logger.info(f"PFP check result: match={match}, distance={dist}, threshold={PFP_HASH_THRESHOLD}")
                    if match:
                         await ban_and_log(update, context, user, "pfp", matched_val, dist, whitelisted, conn)
                         return
            
            logger.info(f"No impersonation detected for user {user.id}")

    except Exception as e:
        logger.error(f"Error checking user: {e}")
    finally:
        conn.close()

async def ban_and_log(update: Update, context: ContextTypes.DEFAULT_TYPE, user, match_type, matched_val, score, whitelisted_rows, conn):
    chat = update.effective_chat
    
    # Determine who was impersonated
    target_id = None
    target_name = "Unknown"
    
    if match_type == 'username':
        target = next((row for row in whitelisted_rows if row['username'] == matched_val), None)
    elif match_type == 'name':
        target = next((row for row in whitelisted_rows if f"{row['first_name']} {row['last_name'] or ''}".strip() == matched_val), None)
    elif match_type == 'pfp':
        target = next((row for row in whitelisted_rows if row['pfp_hash'] == matched_val), None)
    else:
        target = None
        
    if target:
        target_id = target['user_id']
        target_name = f"{target['first_name']} {target['last_name'] or ''}"

    try:
        await chat.ban_member(user.id)
        action = "banned"
        logger.info(f"Banned user {user.id} for {match_type} similarity.")
        await chat.send_message(f"🚫 Banned {user.mention_html()} for impersonating {target_name}.", parse_mode='HTML')
    except Exception as e:
        action = f"failed_ban: {e}"
        logger.error(f"Failed to ban user {user.id}: {e}")

    # Log to DB
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO logs (user_id, target_user_id, detection_type, similarity_score, action_taken, details)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user.id, target_id, match_type, score, action, f"Matched: {matched_val}"))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to log to DB: {e}")

    # Send log to channel
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=f"🚨 **Impersonation Detected** 🚨\n\n"
                     f"User: {user.mention_html()} (ID: {user.id})\n"
                     f"Target: {target_name} (ID: {target_id})\n"
                     f"Reason: Similar {match_type}\n"
                     f"Match: {matched_val}\n"
                     f"Score: {score}\n"
                     f"Action: {action}",
                parse_mode='HTML'
            )
        except Exception as e:
             logger.error(f"Failed to send log to channel: {e}")
