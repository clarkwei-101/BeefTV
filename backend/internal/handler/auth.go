package handler

import (
	"net/http"
	"strings"
	"time"

	"infinite-canvas/backend/internal/app"
	"infinite-canvas/backend/internal/model"

	"github.com/gin-gonic/gin"
)

// sessionCookieName 是浏览器侧的会话凭据名。
//
// 为什么需要 cookie：资源文件接口必须鉴权（不能为了让 <img> 显示就把用户
// 素材变成公开可读），但 <img src> / <video src> 这类媒体标签不会带
// Authorization 头，媒体标签只能靠浏览器自动携带的 cookie 认证。
//
// 安全边界：cookie 只在 GET/HEAD 上被接受，写操作仍然只认 Bearer token；
// cookie 是 SameSite=Lax，跨站子资源请求不会带上它，因此不引入 CSRF 面。
const sessionCookieName = "beeftv_session"

// RegisterAuthRoutes 挂载公网账号路由：注册 / 登录 / 登出 / 当前会话。
// 与桌面本地工作区路由互不干扰：认证走 Authorization: Bearer，不读 cookie。
// profile 是 bootstrap 的运行形态（"server" | "desktop"），随 /auth/status
// 返回给前端做桌面豁免：桌面本机形态跳过登录守卫，server 形态强制登录。
// allowRegistration 控制 /auth/register 是否开放：公网部署通过
// CANVAS_REGISTRATION_ENABLED=false 关掉公开注册（首个管理员照常可建）。
func RegisterAuthRoutes(r *gin.RouterGroup, svc *app.Service, profile string, allowRegistration bool) {
	r.GET("/auth/status", func(c *gin.Context) {
		count, err := svc.AccountCount()
		if err != nil {
			failService(c, err)
			return
		}
		me := accountFromBearer(c, svc)
		ok(c, gin.H{"registered": count > 0, "account": me, "profile": profile, "registrationOpen": allowRegistration || count == 0})
	})
	r.POST("/auth/register", func(c *gin.Context) {
		var req struct {
			Username string `json:"username"`
			Password string `json:"password"`
		}
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
		if err := c.ShouldBindJSON(&req); err != nil {
			fail(c, http.StatusBadRequest, app.BadAuthRequest("请求格式错误"))
			return
		}
		// 公开注册关闭时，只允许建首个管理员账号；之后的新账号由管理员在站内建。
		if !allowRegistration {
			count, err := svc.AccountCount()
			if err != nil {
				failService(c, err)
				return
			}
			if count > 0 {
				fail(c, http.StatusForbidden, app.BadAuthRequest("公开注册已关闭"))
				return
			}
		}
		result, err := svc.RegisterAccount(req.Username, req.Password)
		if err != nil {
			failService(c, err)
			return
		}
		setSessionCookie(c, result.Token, result.ExpiresAt)
		ok(c, result)
	})
	r.POST("/auth/login", func(c *gin.Context) {
		var req struct {
			Username string `json:"username"`
			Password string `json:"password"`
		}
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
		if err := c.ShouldBindJSON(&req); err != nil {
			fail(c, http.StatusBadRequest, app.BadAuthRequest("请求格式错误"))
			return
		}
		result, err := svc.LoginAccount(req.Username, req.Password)
		if err != nil {
			failService(c, err)
			return
		}
		setSessionCookie(c, result.Token, result.ExpiresAt)
		ok(c, result)
	})
	r.POST("/auth/logout", func(c *gin.Context) {
		_ = svc.LogoutSession(sessionCredential(c))
		clearSessionCookie(c)
		ok(c, gin.H{"loggedOut": true})
	})
	r.GET("/auth/me", func(c *gin.Context) {
		account, err := svc.AuthenticateSession(sessionCredential(c))
		if err != nil {
			failService(c, err)
			return
		}
		ok(c, gin.H{"account": app.AuthAccount{ID: account.ID, Username: account.Username, Role: account.Role, Status: account.Status, CreatedAt: account.CreatedAt}})
	})
}

// requestIsHTTPS 判断这次请求在浏览器侧是不是 HTTPS。nginx 反代时会把
// X-Forwarded-Proto 传过来，据此决定 cookie 是否带 Secure。
func requestIsHTTPS(c *gin.Context) bool {
	if c == nil || c.Request == nil {
		return false
	}
	if c.Request.TLS != nil {
		return true
	}
	return strings.EqualFold(strings.TrimSpace(c.GetHeader("X-Forwarded-Proto")), "https")
}

// setSessionCookie 把会话 token 写进 HttpOnly cookie，供媒体标签等无法带
// Authorization 头的请求使用。
func setSessionCookie(c *gin.Context, token string, expiresAt time.Time) {
	if strings.TrimSpace(token) == "" {
		return
	}
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     sessionCookieName,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		Secure:   requestIsHTTPS(c),
		SameSite: http.SameSiteLaxMode,
		Expires:  expiresAt.UTC(),
	})
}

// clearSessionCookie 让登出后浏览器不再携带会话凭据。
func clearSessionCookie(c *gin.Context) {
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     sessionCookieName,
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		Secure:   requestIsHTTPS(c),
		SameSite: http.SameSiteLaxMode,
		MaxAge:   -1,
		Expires:  time.Unix(0, 0).UTC(),
	})
}

// bearerToken 从 Authorization: Bearer 头提取会话 token。
func bearerToken(c *gin.Context) string {
	header := strings.TrimSpace(c.GetHeader("Authorization"))
	if header == "" {
		return ""
	}
	if len(header) > 7 && strings.EqualFold(header[:7], "bearer ") {
		return strings.TrimSpace(header[7:])
	}
	return ""
}

// sessionCredential 取本次请求的会话凭据：优先 Authorization 头，其次是
// 只读请求上的会话 cookie。
//
// cookie 只在 GET/HEAD 上被接受：媒体标签（<img src> / <video src>）没法带
// Authorization 头，只能靠浏览器自动携带 cookie；而写操作一律要求显式的
// Bearer token，配合 SameSite=Lax 不存在跨站伪造面。
func sessionCredential(c *gin.Context) string {
	if token := bearerToken(c); token != "" {
		return token
	}
	if c == nil || c.Request == nil {
		return ""
	}
	if c.Request.Method != http.MethodGet && c.Request.Method != http.MethodHead {
		return ""
	}
	cookie, err := c.Request.Cookie(sessionCookieName)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(cookie.Value)
}

func accountFromBearer(c *gin.Context, svc *app.Service) any {
	account, err := svc.AuthenticateSession(sessionCredential(c))
	if err != nil {
		return nil
	}
	return gin.H{"account": publicAccountJSON(account)}
}

// publicAccountJSON 返回登录账号的公开信息（不含密码哈希）。
func publicAccountJSON(account *model.Account) app.AuthAccount {
	return app.AuthAccount{
		ID: account.ID, Username: account.Username,
		Role: account.Role, Status: account.Status, CreatedAt: account.CreatedAt,
	}
}
