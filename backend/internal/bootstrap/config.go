package bootstrap

import (
	"os"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

type Profile string

const (
	ProfileServer  Profile = "server"
	ProfileDesktop Profile = "desktop"
)

// RegistrationEnabledEnv 是公开注册开关：CANVAS_REGISTRATION_ENABLED=false 时
// /auth/register 只允许建首个管理员账号。默认 true（桌面本机无影响，server 才挂 auth 中间件）。
const RegistrationEnabledEnv = "CANVAS_REGISTRATION_ENABLED"

type Config struct {
	Profile           Profile
	DataDir           string
	DatabaseDriver    string
	DatabaseURL       string
	ListenAddr        string
	LaunchToken       string
	AllowRegistration bool
	AutoMigrate       bool
	ShutdownTimeout   time.Duration
	RouterMiddleware  []gin.HandlerFunc
}

// RegistrationEnabled 解析注册开关：显式设为 false/0/no 才关闭，其余（含未设置）默认开放。
func RegistrationEnabled() bool {
	raw := strings.ToLower(strings.TrimSpace(os.Getenv(RegistrationEnabledEnv)))
	switch raw {
	case "false", "0", "no", "off":
		return false
	default:
		return true
	}
}

func (c Config) withDefaults() Config {
	if c.Profile == "" {
		c.Profile = ProfileServer
	}
	if c.DatabaseDriver == "" {
		c.DatabaseDriver = "sqlite"
	}
	if c.ListenAddr == "" {
		if c.Profile == ProfileDesktop {
			c.ListenAddr = "127.0.0.1:0"
		} else {
			c.ListenAddr = ":8080"
		}
	}
	if c.ShutdownTimeout <= 0 {
		c.ShutdownTimeout = 10 * time.Minute
	}
	return c
}
