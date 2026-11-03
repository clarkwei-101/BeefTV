package model

import "time"

// Account 是公网部署的登录主体：用户名 + bcrypt 密码哈希。首个注册的账号自动
// 成为管理员。与本地单用户工作区的 User 投影互不干扰：Account 只管“谁能登录”，
// User 仍是本地资源归属的 owner 占位。
type Account struct {
	ID           string     `json:"id" gorm:"primaryKey;size:36"`
	Username     string     `json:"username" gorm:"size:64;uniqueIndex;not null"`
	PasswordHash string     `json:"-" gorm:"size:255;not null"`
	Role         UserRole   `json:"role" gorm:"size:24;not null"`
	Status       UserStatus `json:"status" gorm:"size:24;not null"`
	CreatedAt    time.Time  `json:"createdAt"`
	UpdatedAt    time.Time  `json:"updatedAt"`
}

// AuthSession 是不透明 token 会话。token 本身只存哈希，泄露数据库也无法伪造。
type AuthSession struct {
	ID        string    `json:"id" gorm:"primaryKey;size:36"`
	AccountID string    `json:"accountId" gorm:"size:36;index;not null"`
	TokenHash string    `json:"-" gorm:"size:128;uniqueIndex;not null"`
	ExpiresAt time.Time `json:"expiresAt;not null"`
	CreatedAt time.Time `json:"createdAt"`
}
