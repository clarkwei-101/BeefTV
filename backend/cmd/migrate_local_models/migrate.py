#!/usr/bin/env python3
"""
Migration tool: import local-model-config.json channels into the hosted SQLite database.

Usage:
    python3 backend/cmd/migrate_local_models/migrate.py
    python3 backend/cmd/migrate_local_models/migrate.py /path/to/db /path/to/config.json
"""
import sqlite3
import json
import sys
import os
import time
import random
import string

def new_id():
    """Generate a simple unique ID."""
    chars = string.ascii_lowercase + string.digits
    suffix = ''.join(random.choice(chars) for _ in range(16))
    ts = time.strftime('%Y%m%d%H%M%S')
    return f"{ts}{suffix}"

def bool_val(v):
    return bool(v) if v is not None else True

def infer_capability(model_name):
    name = model_name.lower()
    if any(x in name for x in ['flux', 'sdxl', 'dalle', 'stable', 'image']):
        return 'image'
    if any(x in name for x in ['video', 'wan', 'minimax', 'h3', 'sora', 'seedance']):
        return 'video'
    if any(x in name for x in ['audio', 'piper', 'tts']):
        return 'audio'
    return 'text'

def normalize_protocol(protocol, capability):
    p = protocol.lower()
    if 'gemini' in p:
        return 'gemini-image'
    if 'jimeng' in p or 'volc' in p:
        return 'volcengine-jimeng-image'
    if 'openai' in p and capability == 'image':
        return 'openai-image'
    if 'openai' in p and capability == 'video':
        return 'newapi'
    if capability == 'video':
        return 'newapi'
    if capability == 'image':
        return 'openai-image'
    if capability == 'audio':
        return 'chat-completion'
    return 'chat-completion'

def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = os.path.join(repo_root, 'backend', 'data', 'open_ai_canvas.db')
    
    if len(sys.argv) > 2:
        config_path = sys.argv[2]
    else:
        config_path = os.path.join(repo_root, 'backend', 'data', 'local-model-config.json')

    # Read config
    with open(config_path) as f:
        snapshot = json.load(f)

    channels = snapshot.get('config', {}).get('channels', [])
    print(f"读取到 {len(channels)} 个渠道配置")

    # Open DB
    db = sqlite3.connect(db_path)
    cursor = db.cursor()

    # Verify tables
    for table in ['model_channels', 'channel_models', 'channel_model_variants']:
        cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if cursor.fetchone()[0] == 0:
            print(f"错误: 表 {table} 不存在，请先运行后端创建迁移")
            sys.exit(1)

    # Check existing
    cursor.execute("SELECT COUNT(*) FROM model_channels")
    existing_channels = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM channel_models")
    existing_models = cursor.fetchone()[0]
    print(f"当前数据库状态: {existing_channels} 个渠道, {existing_models} 个模型")

    if existing_channels > 0:
        ans = input("数据库已有数据，是否清空后重新导入？(y/N): ")
        if ans.lower() != 'y':
            print("取消")
            sys.exit(0)
        cursor.execute("DELETE FROM channel_model_variants")
        cursor.execute("DELETE FROM channel_models")
        cursor.execute("DELETE FROM model_channels")
        db.commit()
        print("已清空现有数据")

    # Build profile map
    profile_map = {}
    for ch in channels:
        for p in ch.get('modelProfiles', []):
            profile_map[f"{ch['id']}::{p['model']}"] = p

    now = time.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    for ch in channels:
        if not bool_val(ch.get('enabled', True)):
            print(f"  跳过禁用渠道: {ch.get('name', ch.get('id', '?'))}")
            continue
        
        ch_id = ch.get('id', '').strip()
        if not ch_id:
            continue
        
        # Skip beefapi - needs live connection
        if ch_id == 'beefapi':
            print(f"  跳过 beefapi (需重新连接)")
            continue

        scope = ch.get('scope', 'system')
        if scope == 'user':
            scope = 'user'
        else:
            scope = 'system'

        channel_db_id = new_id()
        models_json = json.dumps(ch.get('models', []))

        cursor.execute('''
            INSERT INTO model_channels
            (id, user_id, scope, enabled, name, public_alias, sort_order, base_url, api_key, secret_key, api_format, concurrency_limit, models_json, retired_models_json, headers_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            channel_db_id, '', scope, 1,
            ch.get('name', ch_id), '',
            ch.get('sortOrder', 0),
            ch.get('baseUrl', ''),
            ch.get('apiKey', ''),
            ch.get('secretKey', ''),
            ch.get('apiFormat', 'openai'),
            0, models_json, '[]', '[]',
            now, now
        ))
        print(f"  插入渠道: {ch.get('name', ch_id)} (base_url={ch.get('baseUrl', '')})")

        # Insert models
        for i, model_name in enumerate(ch.get('models', [])):
            model_db_id = new_id()
            
            profile = profile_map.get(f"{ch_id}::{model_name}", {})
            display_name = profile.get('displayName', '') or model_name
            capability = profile.get('capability', '') or infer_capability(model_name)
            protocol = profile.get('protocol', '') or normalize_protocol('', capability)

            cursor.execute('''
                INSERT INTO channel_models
                (id, channel_id, model_key, provider_model_key, display_name, sort_order, icon, capability, protocol, enabled, capability_config_json, capability_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                model_db_id, channel_db_id,
                model_name, model_name,
                display_name, i, '',
                capability, protocol, 1,
                '{}', 0, now, now
            ))
            print(f"    模型: {model_name} (capability={capability}, protocol={protocol})")

            # Insert default variant for video models
            if capability == 'video':
                variant_id = new_id()
                cursor.execute('''
                    INSERT INTO channel_model_variants
                    (id, channel_model_id, selector_key, selector_json, resolution, video_seconds, provider_model_key, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    variant_id, model_db_id,
                    '{"operation":"text_to_video"}',
                    '{"operation":"text_to_video"}',
                    '720p', 6, model_name, 1,
                    now, now
                ))

    db.commit()

    # Summary
    cursor.execute("SELECT COUNT(*) FROM model_channels")
    final_channels = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM channel_models")
    final_models = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM channel_model_variants")
    final_variants = cursor.fetchone()[0]
    db.close()

    print(f"\n迁移完成: {final_channels} 个渠道, {final_models} 个模型, {final_variants} 个变体")
    print("\n重启后端后创作端应能看到以下渠道:")
    print("  - dgx-flux-local     → FLUX.1 Schnell (图片)")
    print("  - dgx-sdxl-local     → SDXL Base (图片)")
    print("  - minimax-h3-local  → MiniMax H3 (视频)")
    print("  - mac-ollama-local   → Qwen3 8B (文本)")
    print("  - mac-piper-local    → Piper TTS (音频)")
    print("\n如果生成失败，检查各服务端口:")
    print("  - DGX Spark ComfyUI : http://localhost:8198")
    print("  - DGX Spark LLM    : http://192.168.1.193:8000")
    print("  - Mac Ollama        : http://localhost:11434")

if __name__ == '__main__':
    main()
