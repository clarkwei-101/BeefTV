package handler

import (
	"net/http"
	"strings"

	"infinite-canvas/backend/internal/agentops"
	"infinite-canvas/backend/internal/app"
	"infinite-canvas/backend/internal/model"

	"github.com/gin-gonic/gin"
)

// authSessionKey 是会话中间件注入已认证账号的 context 键。
const authSessionKey = "beeftv.auth.account"

// publicAuthPaths 是无需登录即可访问的 API 路径（相对 /api）。
var publicAuthPaths = map[string]bool{
	"/auth/status":   true,
	"/auth/register": true,
	"/auth/login":    true,
	"/openapi.yaml":  true,
}

// ServerAuthMiddleware 是 server profile 的会话守卫：除公开路径外，所有
// /api 请求必须携带有效的 Authorization: Bearer 会话 token。desktop profile
// 不挂载此中间件，保持本机单用户零登录体验。
func ServerAuthMiddleware(svc *app.Service) gin.HandlerFunc {
	return func(c *gin.Context) {
		path := strings.TrimPrefix(c.Request.URL.Path, "/api")
		if publicAuthPaths[path] || strings.HasPrefix(path, "/auth/") && isAuthPublicPath(path) {
			c.Next()
			return
		}
		if strings.HasPrefix(path, "/auth/") && !isAuthPublicPath(path) {
			// /auth/me、/auth/logout 等：无 token 时由 handler 返回 401，
			// 这里不提前拦截以保持错误语义统一。
			c.Next()
			return
		}
		if isReadOnlyPublicPath(c.Request.Method, path) {
			c.Next()
			return
		}
		if hasMachineCredential(c, svc) {
			c.Next()
			return
		}
		account, err := svc.AuthenticateSession(sessionCredential(c))
		if err != nil {
			failService(c, err)
			c.Abort()
			return
		}
		c.Set(authSessionKey, account)
		c.Next()
	}
}

// hasMachineCredential 判定请求是否出示了本机机器凭据：owner 凭据或内置助手宿主凭据。
// 两者都是数据目录里 0600 的秘密，只注入本机进程（CLI/MCP/助手宿主），
// 等价于"已经登录"的机器身份。
// 这里不能用来源地址代替凭据：公网形态的 Web 层也是从本机回环反代到后端的，
// 任何公网请求在后端看来都是 loopback，位置判断挡不住公网流量；
// 真正的边界是"拿不到本机秘密"。
func hasMachineCredential(c *gin.Context, svc *app.Service) bool {
	if agentops.OwnerTokenMatches(svc.DataDir(), strings.TrimSpace(c.GetHeader("X-Beeftv-Owner"))) {
		return true
	}
	return agentops.HostTokenMatches(svc.DataDir(), strings.TrimSpace(c.GetHeader(assistantHostTokenHeader)))
}

// isAuthPublicPath 判定 /auth 下的公开路径（status/register/login）。
func isAuthPublicPath(path string) bool {
	return path == "/auth/status" || path == "/auth/register" || path == "/auth/login"
}

// isReadOnlyPublicPath 判定无需登录的只读路径：健康检查与版本信息。
// 写操作（POST/PUT/PATCH/DELETE）一律要求会话。
func isReadOnlyPublicPath(method, path string) bool {
	if method != http.MethodGet && method != http.MethodHead && method != http.MethodOptions {
		return false
	}
	// 健康检查由 deploy/scripts 下的探针和 watchdog 轮询，路径带子段
	// （/health/live、/health/ready、/health/startup），必须整段前缀放行，
	// 否则探针拿到 401 会把健康的后端误判成 DOWN。
	if path == "/health" || strings.HasPrefix(path, "/health/") {
		return true
	}
	if path == "/version" || path == "/status" {
		return true
	}
	if strings.HasPrefix(path, "/system/") {
		return true
	}
	return false
}

// SessionAccount 返回当前请求的已认证账号（中间件已注入时）。
func SessionAccount(c *gin.Context) (*model.Account, bool) {
	value, exists := c.Get(authSessionKey)
	if !exists {
		return nil, false
	}
	account, ok := value.(*model.Account)
	if !ok || account == nil {
		return nil, false
	}
	return account, true
}
