package database

import (
	"fmt"

	"infinite-canvas/backend/internal/model"

	"gorm.io/gorm"
)

// requirePublicAuthSchema 校验 v14 公网账号结构：账号表、会话表、关键列与
// 索引必须完整。登录能力依赖这些结构，缺列或缺索引时不能报告 Ready。
func requirePublicAuthSchema(db *gorm.DB) error {
	if !db.Migrator().HasTable(&model.Account{}) {
		return fmt.Errorf("本地账号表缺失，请启用自动迁移")
	}
	if !db.Migrator().HasTable(&model.AuthSession{}) {
		return fmt.Errorf("本地账号会话表缺失，请启用自动迁移")
	}
	for _, column := range []string{"id", "username", "password_hash", "role", "status", "created_at", "updated_at"} {
		has, err := sqliteHasColumn(db, "accounts", column)
		if err != nil {
			return err
		}
		if !has {
			return fmt.Errorf("本地账号表缺失列 %s", column)
		}
	}
	for _, column := range []string{"id", "account_id", "token_hash", "expires_at", "created_at"} {
		has, err := sqliteHasColumn(db, "auth_sessions", column)
		if err != nil {
			return err
		}
		if !has {
			return fmt.Errorf("本地账号会话表缺失列 %s", column)
		}
	}
	if err := requireSQLitePrimaryKey(db, "accounts", []string{"id"}); err != nil {
		return err
	}
	if err := requireSQLitePrimaryKey(db, "auth_sessions", []string{"id"}); err != nil {
		return err
	}
	for _, index := range []sqliteIndexContract{
		{table: "accounts", name: "idx_accounts_username", columns: []string{"username"}, unique: true},
		{table: "auth_sessions", name: "idx_auth_sessions_token_hash", columns: []string{"token_hash"}, unique: true},
		{table: "auth_sessions", name: "idx_auth_sessions_account_id", columns: []string{"account_id"}, unique: false},
	} {
		valid, err := matchesSQLiteIndex(db, index)
		if err != nil {
			return err
		}
		if !valid {
			return fmt.Errorf("本地数据库索引 %s 定义不完整", index.name)
		}
	}
	return nil
}
