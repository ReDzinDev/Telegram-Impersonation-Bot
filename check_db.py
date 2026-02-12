import sys
sys.path.insert(0, 'd:/Antigravity/Anti Impersonator Bot')

from src.db import get_connection

try:
    conn = get_connection()
    if not conn:
        print("❌ Failed to connect to database")
        sys.exit(1)
    
    print("✅ Database connected successfully")
    
    with conn.cursor() as cur:
        # Check whitelisted users
        cur.execute('SELECT COUNT(*) as count FROM whitelisted_users')
        count = cur.fetchone()['count']
        print(f"\n📊 Total whitelisted users: {count}")
        
        if count > 0:
            cur.execute('SELECT user_id, username, first_name, last_name, pfp_hash FROM whitelisted_users LIMIT 5')
            rows = cur.fetchall()
            print("\n👥 Sample whitelisted users:")
            for r in rows:
                pfp_status = "✅ Has PFP" if r['pfp_hash'] else "❌ No PFP"
                print(f"  - ID: {r['user_id']}, Username: @{r['username']}, Name: {r['first_name']} {r['last_name'] or ''}, {pfp_status}")
                if r['pfp_hash']:
                    print(f"    PFP Hash: {r['pfp_hash'][:16]}...")
        else:
            print("\n⚠️  No users in whitelist! Run /import_admins first.")
    
    conn.close()
    print("\n✅ Database check complete")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
