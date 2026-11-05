// +build ignore

// Migration tool: import local-model-config.json channels into the hosted SQLite database.
// Run: go run backend/cmd/migrate_local_models/main.go
package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

// Snapshot from local-model-config.json
type Snapshot struct {
	SchemaVersion int           `json:"schemaVersion"`
	Config       ConfigSection  `json:"config"`
}

type ConfigSection struct {
	Channels []Channel `json:"channels"`
}

type Channel struct {
	ID           string         `json:"id"`
	Name         string         `json:"name"`
	Scope        string         `json:"scope"` // "user" or "system"
	BaseURL      string         `json:"baseUrl"`
	APIKey       string         `json:"apiKey"`
	SecretKey    string         `json:"secretKey"`
	APIFormat    string         `json:"apiFormat"`
	Enabled      *bool          `json:"enabled"`
	SortOrder    int            `json:"sortOrder"`
	Pinned       *bool          `json:"pinned"`
	ModelProfiles []ModelProfile `json:"modelProfiles"`
	Models       []string       `json:"models"`
}

type ModelProfile struct {
	Model      string `json:"model"`
	DisplayName string `json:"displayName"`
	Capability string `json:"capability"`
	Protocol   string `json:"protocol"`
}

func newID() string {
	// Simple ULID-like ID using timestamp + random
	return fmt.Sprintf("%s%s%s",
		time.Now().Format("20060102150405"),
		strings.ReplaceAll(randomString(8), "-", ""),
		strings.ReplaceAll(randomString(8), "-", ""))
}

// randomString 生成 n 位随机字母数字串。
//
// 旧实现用 time.Now().UnixNano()%62 取字符，但循环体里没有推进时钟，
// 同一纳秒内多次调用会得到完全相同的字符串，于是连续插入的记录生成了
// 相同 ID，触发 UNIQUE constraint failed: model_channels.id 而整批回滚。
// 这里改用 math/rand 并显式播种，ID 才有足够的区分度。
func randomString(n int) string {
	const letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	b := make([]byte, n)
	for i := range b {
		b[i] = letters[rand.Intn(len(letters))]
	}
	return string(b)
}

func boolVal(b *bool) bool {
	if b == nil {
		return true
	}
	return *b
}

func main() {
	// 播种随机数源：math/rand 默认是确定性的，同一进程内每次运行会生成
	// 完全相同的 ID 序列。
	rand.Seed(time.Now().UnixNano())

	// Resolve paths
	backendDir := "backend"
	dataDir := filepath.Join(backendDir, "data")
	dbPath := filepath.Join(dataDir, "open_ai_canvas.db")
	configPath := filepath.Join(dataDir, "local-model-config.json")

	if len(os.Args) > 1 {
		dbPath = os.Args[1]
	}
	if len(os.Args) > 2 {
		configPath = os.Args[2]
	}

	// Read config
	configData, err := os.ReadFile(configPath)
	if err != nil {
		log.Fatalf("读取配置文件失败: %v", err)
	}

	var snapshot Snapshot
	if err := json.Unmarshal(configData, &snapshot); err != nil {
		log.Fatalf("解析配置文件失败: %v", err)
	}

	log.Printf("读取到 %d 个渠道配置", len(snapshot.Config.Channels))

	// Open DB
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		log.Fatalf("打开数据库失败: %v", err)
	}
	defer db.Close()

	// Verify tables exist
	tables := []string{"model_channels", "channel_models", "channel_model_variants"}
	for _, t := range tables {
		var count int
		if err := db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", t).Scan(&count); err != nil {
			log.Fatalf("检查表 %s 失败: %v", t, err)
		}
		if count == 0 {
			log.Fatalf("表 %s 不存在，请先运行一次后端让迁移创建表", t)
		}
	}

	// Count existing
	var existingChannels int
	db.QueryRow("SELECT COUNT(*) FROM model_channels").Scan(&existingChannels)
	var existingModels int
	db.QueryRow("SELECT COUNT(*) FROM channel_models").Scan(&existingModels)
	log.Printf("当前数据库状态: %d 个渠道, %d 个模型", existingChannels, existingModels)

	if existingChannels > 0 {
		fmt.Println("数据库已有渠道数据，是否覆盖？(y/N): ")
		var ans string
		fmt.Scanln(&ans)
		if ans != "y" && ans != "Y" {
			log.Fatal("取消")
		}
		// Delete existing
		if _, err := db.Exec("DELETE FROM channel_model_variants"); err != nil {
			log.Fatalf("清空 channel_model_variants: %v", err)
		}
		if _, err := db.Exec("DELETE FROM channel_models"); err != nil {
			log.Fatalf("清空 channel_models: %v", err)
		}
		if _, err := db.Exec("DELETE FROM model_channels"); err != nil {
			log.Fatalf("清空 model_channels: %v", err)
		}
		log.Println("已清空现有数据")
	}

	now := time.Now().UTC().Format("2006-01-02T15:04:05.000Z")

	// Profile map for display name resolution
	profileMap := map[string]ModelProfile{}
	for _, ch := range snapshot.Config.Channels {
		for _, p := range ch.ModelProfiles {
			profileMap[ch.ID+"::"+p.Model] = p
		}
	}

	tx, err := db.Begin()
	if err != nil {
		log.Fatalf("开启事务失败: %v", err)
	}

	for _, ch := range snapshot.Config.Channels {
		if !boolVal(ch.Enabled) {
			continue
		}
		if strings.TrimSpace(ch.ID) == "" {
			continue
		}
		// Skip BeefAPI - it needs a live connection
		if ch.ID == "beefapi" {
			log.Printf("跳过 beefapi (需重新连接)")
			continue
		}

		// Determine scope
		scope := "system"
		if ch.Scope == "user" {
			scope = "user"
		}

		// Insert channel
		//
		// 渠道 ID 必须沿用 local-model-config.json 里的稳定 id（如 minimax-h3-local），
		// 不能用随机 ULID：用户配置（imageModel/videoModel/...）引用的是这些稳定 id，
		// 随机 ID 会让引用解析不到，接口直接返回"必须指定系统渠道和模型"。
		channelID := ch.ID
		modelsJSON := "[]"
		if len(ch.Models) > 0 {
			b, _ := json.Marshal(ch.Models)
			modelsJSON = string(b)
		}

		_, err := tx.Exec(`INSERT INTO model_channels
			(id, user_id, scope, enabled, name, public_alias, sort_order, base_url, api_key, secret_key, api_format, concurrency_limit, models_json, retired_models_json, headers_json, created_at, updated_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			channelID, "", scope, 1, ch.Name, ch.ID, ch.SortOrder, ch.BaseURL, ch.APIKey, ch.SecretKey, ch.APIFormat, 0, modelsJSON, "[]", "[]", now, now)
		if err != nil {
			tx.Rollback()
			log.Fatalf("插入渠道 %s (%s) 失败: %v", ch.Name, ch.ID, err)
		}
		log.Printf("  插入渠道: %s (id=%s base_url=%s)", ch.Name, channelID, ch.BaseURL)

		// Insert models
		for i, modelName := range ch.Models {
			modelID := newID()
			displayName := modelName
			capability := ""
			protocol := "openai"

			// Try to get from profile
			if p, ok := profileMap[ch.ID+"::"+modelName]; ok {
				if p.DisplayName != "" {
					displayName = p.DisplayName
				}
				capability = p.Capability
				protocol = p.Protocol
			}

			if capability == "" {
				// Infer from model name
				switch {
				case strings.Contains(strings.ToLower(modelName), "image") || strings.Contains(strings.ToLower(modelName), "flux") || strings.Contains(strings.ToLower(modelName), "sdxl") || strings.Contains(strings.ToLower(modelName), "dalle"):
					capability = "image"
				case strings.Contains(strings.ToLower(modelName), "video") || strings.Contains(strings.ToLower(modelName), "wan") || strings.Contains(strings.ToLower(modelName), "minimax") || strings.Contains(strings.ToLower(modelName), "h3") || strings.Contains(strings.ToLower(modelName), "sora"):
					capability = "video"
				case strings.Contains(strings.ToLower(modelName), "audio") || strings.Contains(strings.ToLower(modelName), "piper") || strings.Contains(strings.ToLower(modelName), "tts"):
					capability = "audio"
				case strings.Contains(strings.ToLower(modelName), "qwen") || strings.Contains(strings.ToLower(modelName), "deepseek") || strings.Contains(strings.ToLower(modelName), "gpt") || strings.Contains(strings.ToLower(modelName), "llama") || strings.Contains(strings.ToLower(modelName), "claude"):
					capability = "text"
				default:
					capability = "text"
				}
			}

			// Normalize protocol to known values
			switch {
			case strings.Contains(strings.ToLower(protocol), "gemini"):
				protocol = "gemini-image"
			case strings.Contains(strings.ToLower(protocol), "jimeng") || strings.Contains(strings.ToLower(protocol), "volc"):
				protocol = "volcengine-jimeng-image"
			case strings.Contains(strings.ToLower(protocol), "openai") && capability == "image":
				protocol = "openai-image"
			case strings.Contains(strings.ToLower(protocol), "openai") && capability == "video":
				protocol = "newapi"
			default:
				if protocol == "" || protocol == "openai" {
					if capability == "video" {
						protocol = "newapi"
					} else if capability == "image" {
						protocol = "openai-image"
					} else {
						protocol = "chat-completion"
					}
				}
			}

			_, err := tx.Exec(`INSERT INTO channel_models
				(id, channel_id, model_key, provider_model_key, display_name, sort_order, icon, capability, protocol, enabled, capability_config_json, capability_version, created_at, updated_at)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
				modelID, channelID, modelName, modelName, displayName, i, "", capability, protocol, 1, "{}", 0, now, now)
			if err != nil {
				tx.Rollback()
				log.Fatalf("插入模型 %s 失败: %v", modelName, err)
			}
			log.Printf("    模型: %s (capability=%s, protocol=%s)", modelName, capability, protocol)

			// For video models, insert a default variant
			if capability == "video" {
				variantID := newID()
				_, err := tx.Exec(`INSERT INTO channel_model_variants
					(id, channel_model_id, selector_key, selector_json, resolution, video_seconds, provider_model_key, enabled, created_at, updated_at)
					VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
					variantID, modelID, `{"operation":"text_to_video"}`, `{"operation":"text_to_video"}`, "720p", 6, modelName, 1, now, now)
				if err != nil {
					tx.Rollback()
					log.Fatalf("插入视频变体 %s 失败: %v", modelName, err)
				}
			}
		}
	}

	if err := tx.Commit(); err != nil {
		log.Fatalf("提交事务失败: %v", err)
	}

	// Summary
	var finalChannels, finalModels, finalVariants int
	db.QueryRow("SELECT COUNT(*) FROM model_channels").Scan(&finalChannels)
	db.QueryRow("SELECT COUNT(*) FROM channel_models").Scan(&finalModels)
	db.QueryRow("SELECT COUNT(*) FROM channel_model_variants").Scan(&finalVariants)

	log.Printf("迁移完成: %d 个渠道, %d 个模型, %d 个变体", finalChannels, finalModels, finalVariants)
	fmt.Println("\n提示: 重启后端后，创作端应能看到以下渠道:")
	fmt.Println("  - dgx-flux-local  → FLUX.1 Schnell (图片)")
	fmt.Println("  - dgx-sdxl-local  → SDXL Base (图片)")
	fmt.Println("  - minimax-h3-local → MiniMax H3 (视频)")
	fmt.Println("  - mac-ollama-local → Qwen3 8B (文本)")
	fmt.Println("  - mac-piper-local → Piper TTS (音频)")
	fmt.Println("\n注意: 如果生成失败，检查各服务的 API 端口是否可达:")
	fmt.Println("  - DGX Spark ComfyUI: http://localhost:8198 (图片/视频)")
	fmt.Println("  - DGX Spark LLM:     http://192.168.1.193:8000 (文本)")
	fmt.Println("  - Mac Ollama:         http://localhost:11434 (本地文本)")
}
