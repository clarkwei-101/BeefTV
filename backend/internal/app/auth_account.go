package app

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"strings"
	"time"

	"infinite-canvas/backend/internal/kernel"
	"infinite-canvas/backend/internal/model"

	"golang.org/x/crypto/bcrypt"
)

// AuthAccount 是登录/注册成功后返回的公开账号信息，不含密码哈希。
type AuthAccount struct {
	ID        string           `json:"id"`
	Username  string           `json:"username"`
	Role      model.UserRole   `json:"role"`
	Status    model.UserStatus `json:"status"`
	CreatedAt time.Time        `json:"createdAt"`
}

func publicAuthAccount(value *model.Account) AuthAccount {
	return AuthAccount{
		ID: value.ID, Username: value.Username,
		Role: value.Role, Status: value.Status, CreatedAt: value.CreatedAt,
	}
}

// AuthResult 是会话签发结果：账号 + 不透明 token + 过期时间。
type AuthResult struct {
	Account   AuthAccount `json:"account"`
	Token     string      `json:"token"`
	ExpiresAt time.Time   `json:"expiresAt"`
}

// authSessionTTL 是公网会话有效期。30 天：创作工具是长期使用的桌面型应用，
// 不要求频繁重登；token 本身只存哈希，登出即删。
const authSessionTTL = 30 * 24 * time.Hour

func normalizeUsername(value string) string {
	return strings.ToLower(strings.TrimSpace(value))
}

func validateUsername(username string) error {
	username = normalizeUsername(username)
	if len(username) < 3 || len(username) > 32 {
		return kernel.BadAuthRequest("用户名长度必须在 3-32 个字符之间")
	}
	for _, r := range username {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' || r == '-' {
			continue
		}
		if r > 127 {
			continue // 允许中文等非 ASCII：用户名对用户可见
		}
		return kernel.BadAuthRequest("用户名只能包含字母、数字、下划线、中划线")
	}
	return nil
}

func validatePassword(password string) error {
	if len(password) < 6 || len(password) > 128 {
		return kernel.BadAuthRequest("密码长度必须在 6-128 个字符之间")
	}
	return nil
}

func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return base64.RawStdEncoding.EncodeToString(sum[:])
}

func newSessionToken() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

// issueSession 为账号签发一个新会话。
func (s *Service) issueSession(account *model.Account) (*AuthResult, error) {
	if s.repo == nil {
		return nil, kernel.NewAppError(500, "账号存储未初始化")
	}
	token, err := newSessionToken()
	if err != nil {
		return nil, kernel.NewAppError(500, "会话签发失败")
	}
	now := time.Now().UTC()
	session := &model.AuthSession{
		ID: kernel.NewID(), AccountID: account.ID,
		TokenHash: hashToken(token), ExpiresAt: now.Add(authSessionTTL), CreatedAt: now,
	}
	if err := s.repo.DB().Create(session).Error; err != nil {
		return nil, kernel.NewAppError(500, "会话保存失败")
	}
	return &AuthResult{
		Account: publicAuthAccount(account), Token: token, ExpiresAt: session.ExpiresAt,
	}, nil
}

// RegisterAccount 注册新账号。首个注册的账号自动成为管理员；后续注册均为普通用户。
func (s *Service) RegisterAccount(username, password string) (*AuthResult, error) {
	if s.repo == nil {
		return nil, kernel.NewAppError(500, "账号存储未初始化")
	}
	username = normalizeUsername(username)
	if err := validateUsername(username); err != nil {
		return nil, err
	}
	if err := validatePassword(password); err != nil {
		return nil, err
	}
	var existing model.Account
	if err := s.repo.DB().Where("username = ?", username).First(&existing).Error; err == nil {
		return nil, kernel.NewAppError(409, "用户名已被占用")
	}
	var count int64
	if err := s.repo.DB().Model(&model.Account{}).Count(&count).Error; err != nil {
		return nil, kernel.NewAppError(500, "账号查询失败")
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return nil, kernel.NewAppError(500, "密码加密失败")
	}
	role := model.UserRoleUser
	if count == 0 {
		role = model.UserRoleAdmin
	}
	now := time.Now().UTC()
	account := &model.Account{
		ID: kernel.NewID(), Username: username, PasswordHash: string(hash),
		Role: role, Status: model.UserStatusActive, CreatedAt: now, UpdatedAt: now,
	}
	if err := s.repo.DB().Create(account).Error; err != nil {
		if strings.Contains(err.Error(), "UNIQUE") || strings.Contains(err.Error(), "unique") {
			return nil, kernel.NewAppError(409, "用户名已被占用")
		}
		return nil, kernel.NewAppError(500, "账号创建失败")
	}
	return s.issueSession(account)
}

// LoginAccount 用用户名 + 密码登录。
func (s *Service) LoginAccount(username, password string) (*AuthResult, error) {
	if s.repo == nil {
		return nil, kernel.NewAppError(500, "账号存储未初始化")
	}
	username = normalizeUsername(username)
	var account model.Account
	if err := s.repo.DB().Where("username = ?", username).First(&account).Error; err != nil {
		return nil, kernel.Unauthorized("用户名或密码错误")
	}
	if account.Status != model.UserStatusActive {
		return nil, kernel.Forbidden("账号已被禁用")
	}
	if err := bcrypt.CompareHashAndPassword([]byte(account.PasswordHash), []byte(password)); err != nil {
		return nil, kernel.Unauthorized("用户名或密码错误")
	}
	return s.issueSession(&account)
}

// AuthenticateSession 用 Bearer token 解析会话，返回账号。token 错误/过期返回 401。
func (s *Service) AuthenticateSession(token string) (*model.Account, error) {
	if s.repo == nil {
		return nil, kernel.Unauthorized("账号存储未初始化")
	}
	token = strings.TrimSpace(token)
	if token == "" {
		return nil, kernel.Unauthorized("未登录")
	}
	var session model.AuthSession
	if err := s.repo.DB().Where("token_hash = ?", hashToken(token)).First(&session).Error; err != nil {
		return nil, kernel.Unauthorized("登录已过期，请重新登录")
	}
	if time.Now().UTC().After(session.ExpiresAt) {
		_ = s.repo.DB().Delete(&session).Error
		return nil, kernel.Unauthorized("登录已过期，请重新登录")
	}
	var account model.Account
	if err := s.repo.DB().Where("id = ?", session.AccountID).First(&account).Error; err != nil {
		return nil, kernel.Unauthorized("账号不存在")
	}
	if account.Status != model.UserStatusActive {
		return nil, kernel.Forbidden("账号已被禁用")
	}
	return &account, nil
}

// LogoutSession 吊销一个会话 token。
func (s *Service) LogoutSession(token string) error {
	if s.repo == nil {
		return kernel.NewAppError(500, "账号存储未初始化")
	}
	token = strings.TrimSpace(token)
	if token == "" {
		return nil
	}
	_ = s.repo.DB().Where("token_hash = ?", hashToken(token)).Delete(&model.AuthSession{}).Error
	return nil
}

// AccountCount 返回已注册账号数（用于判断是否需要引导首个管理员注册）。
func (s *Service) AccountCount() (int64, error) {
	if s.repo == nil {
		return 0, errors.New("账号存储未初始化")
	}
	var count int64
	if err := s.repo.DB().Model(&model.Account{}).Count(&count).Error; err != nil {
		return 0, err
	}
	return count, nil
}
