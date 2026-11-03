package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"infinite-canvas/backend/internal/agentops"
	"infinite-canvas/backend/internal/app"
	"infinite-canvas/backend/internal/database"
	"infinite-canvas/backend/internal/repository"

	"github.com/gin-gonic/gin"
)

func newAuthTestRouter(t *testing.T) (*gin.Engine, *app.Service) {
	t.Helper()
	dir := t.TempDir()
	db, err := database.Open(database.Config{Driver: "sqlite", DataDir: dir})
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	if err := database.MigrateLocalSchema(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	repo := repository.New(db)
	svc := app.New(repo, dir)
	gin.SetMode(gin.TestMode)
	router := gin.New()
	api := router.Group("/api")
	RegisterAuthRoutes(api, svc, "server", true)
	router.Use(ServerAuthMiddleware(svc))
	return router, svc
}

func doJSON(t *testing.T, router *gin.Engine, method, path, token string, body any) (int, map[string]any) {
	t.Helper()
	var raw []byte
	if body != nil {
		var err error
		raw, err = json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
	}
	req := httptest.NewRequest(method, path, bytes.NewReader(raw))
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, req)
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal %s %s: %v (%s)", method, path, err, rec.Body.String())
	}
	return rec.Code, out
}

func TestPublicAuthRegisterLoginMe(t *testing.T) {
	router, _ := newAuthTestRouter(t)

	// status：无人注册
	_, status := doJSON(t, router, "GET", "/api/auth/status", "", nil)
	if status["data"].(map[string]any)["registered"] != false {
		t.Fatalf("expected registered=false, got %v", status)
	}

	// 注册首个用户 → 应为 admin
	_, reg := doJSON(t, router, "POST", "/api/auth/register", "", map[string]any{
		"username": "alice", "password": "secret123",
	})
	data := reg["data"].(map[string]any)
	account := data["account"].(map[string]any)
	if account["role"] != "admin" {
		t.Fatalf("first user should be admin, got %v", account)
	}
	token := data["token"].(string)
	if token == "" {
		t.Fatal("empty token")
	}

	// 第二个用户 → 普通 user
	_, reg2 := doJSON(t, router, "POST", "/api/auth/register", "", map[string]any{
		"username": "bob", "password": "secret123",
	})
	if reg2["data"].(map[string]any)["account"].(map[string]any)["role"] != "user" {
		t.Fatalf("second user should be user, got %v", reg2)
	}

	// 重复用户名 → 409
	code, _ := doJSON(t, router, "POST", "/api/auth/register", "", map[string]any{
		"username": "alice", "password": "secret123",
	})
	if code != http.StatusConflict {
		t.Fatalf("expected 409, got %d", code)
	}

	// 登录
	_, login := doJSON(t, router, "POST", "/api/auth/login", "", map[string]any{
		"username": "alice", "password": "secret123",
	})
	loginToken := login["data"].(map[string]any)["token"].(string)
	if loginToken == "" {
		t.Fatal("login returned empty token")
	}

	// 错误密码 → 401
	code, _ = doJSON(t, router, "POST", "/api/auth/login", "", map[string]any{
		"username": "alice", "password": "wrongpass",
	})
	if code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", code)
	}

	// /auth/me：带 token 成功
	_, me := doJSON(t, router, "GET", "/api/auth/me", loginToken, nil)
	if me["data"].(map[string]any)["account"].(map[string]any)["username"] != "alice" {
		t.Fatalf("unexpected me: %v", me)
	}

	// /auth/me：无 token → 401
	code, _ = doJSON(t, router, "GET", "/api/auth/me", "", nil)
	if code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for anonymous /auth/me, got %d", code)
	}

	// 登出后 token 失效
	_, _ = doJSON(t, router, "POST", "/api/auth/logout", loginToken, nil)
	code, _ = doJSON(t, router, "GET", "/api/auth/me", loginToken, nil)
	if code != http.StatusUnauthorized {
		t.Fatalf("expected 401 after logout, got %d", code)
	}
}

// TestPublicAuthSessionCookieCoversMediaReads 覆盖媒体标签的鉴权路径：
// <img src> / <video src> 不会带 Authorization 头，只能靠浏览器自动携带的
// 会话 cookie 读资源。cookie 必须在只读请求上生效，在写请求上必须无效。
func TestPublicAuthSessionCookieCoversMediaReads(t *testing.T) {
	dir := t.TempDir()
	db, err := database.Open(database.Config{Driver: "sqlite", DataDir: dir})
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	if err := database.MigrateLocalSchema(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	svc := app.New(repository.New(db), dir)
	gin.SetMode(gin.TestMode)
	router := gin.New()
	// 会话守卫必须挂在路由之前：媒体读请求是否放行正是这里要验的。
	router.Use(ServerAuthMiddleware(svc))
	api := router.Group("/api")
	RegisterAuthRoutes(api, svc, "server", true)

	register := func() (string, *http.Cookie) {
		req := httptest.NewRequest("POST", "/api/auth/register", bytes.NewReader([]byte(`{"username":"carol","password":"secret123"}`)))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("register failed: %d %s", rec.Code, rec.Body.String())
		}
		var payload struct {
			Data struct {
				Token string `json:"token"`
			} `json:"data"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		for _, cookie := range rec.Result().Cookies() {
			if cookie.Name == sessionCookieName {
				return payload.Data.Token, cookie
			}
		}
		t.Fatal("register did not set the session cookie")
		return "", nil
	}

	token, cookie := register()
	if cookie.Value != token {
		t.Fatalf("cookie value should carry the session token, got %q", cookie.Value)
	}
	if !cookie.HttpOnly {
		t.Fatal("session cookie must be HttpOnly")
	}
	if cookie.SameSite != http.SameSiteLaxMode {
		t.Fatalf("session cookie must be SameSite=Lax, got %v", cookie.SameSite)
	}

	// 只读请求带 cookie → 通过
	req := httptest.NewRequest("GET", "/api/auth/me", nil)
	req.AddCookie(cookie)
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("cookie authenticated GET should pass, got %d %s", rec.Code, rec.Body.String())
	}

	// 写请求带 cookie → 必须拒绝（写路径只认 Bearer token）
	req = httptest.NewRequest("POST", "/api/auth/logout", bytes.NewReader([]byte(`{}`)))
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(cookie)
	rec = httptest.NewRecorder()
	router.ServeHTTP(rec, req)
	if rec.Code == http.StatusUnauthorized {
		t.Fatalf("cookie-only write should be rejected by the session guard, got %d", rec.Code)
	}

	// Bearer 登出后 cookie 也失效
	req = httptest.NewRequest("POST", "/api/auth/logout", bytes.NewReader([]byte(`{}`)))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	rec = httptest.NewRecorder()
	router.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("logout failed: %d %s", rec.Code, rec.Body.String())
	}
	cleared := false
	for _, item := range rec.Result().Cookies() {
		if item.Name == sessionCookieName && item.MaxAge < 0 {
			cleared = true
		}
	}
	if !cleared {
		t.Fatal("logout should clear the session cookie")
	}
}

func TestPublicAuthValidation(t *testing.T) {
	router, _ := newAuthTestRouter(t)
	for _, tc := range []struct {
		name string
		body map[string]any
	}{
		{"short-username", map[string]any{"username": "ab", "password": "secret123"}},
		{"short-password", map[string]any{"username": "alice", "password": "123"}},
		{"empty", map[string]any{"username": "", "password": ""}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			code, _ := doJSON(t, router, "POST", "/api/auth/register", "", tc.body)
			if code != http.StatusBadRequest {
				t.Fatalf("expected 400, got %d", code)
			}
		})
	}
}

// TestServerAuthAllowsMachineCredentials 覆盖 server profile 下的机器身份：
// owner 凭据（CLI/MCP）与宿主凭据（内置助手）必须能通过会话守卫，
// 否则本机代理工具在公网形态里完全不可用；无凭据与错误凭据仍必须 401。
func TestServerAuthAllowsMachineCredentials(t *testing.T) {
	dir := t.TempDir()
	db, err := database.Open(database.Config{Driver: "sqlite", DataDir: dir})
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	if err := database.MigrateLocalSchema(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	svc := app.New(repository.New(db), dir)
	owner, err := agentops.EnsureOwnerToken(dir)
	if err != nil {
		t.Fatalf("owner token: %v", err)
	}
	agentops.EnsureAgentCredentials(dir)
	hostRaw, err := os.ReadFile(filepath.Join(dir, "agent_host_token"))
	if err != nil {
		t.Fatalf("host token: %v", err)
	}
	host := string(hostRaw)

	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.Use(ServerAuthMiddleware(svc))
	router.GET("/api/protected", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"code": 0}) })

	call := func(header, value string) int {
		req := httptest.NewRequest(http.MethodGet, "/api/protected", nil)
		if header != "" {
			req.Header.Set(header, value)
		}
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		return rec.Code
	}

	if code := call("", ""); code != http.StatusUnauthorized {
		t.Fatalf("anonymous request should be 401, got %d", code)
	}
	if code := call("X-Beeftv-Owner", "not-the-owner-token"); code != http.StatusUnauthorized {
		t.Fatalf("wrong owner token should be 401, got %d", code)
	}
	if code := call("X-Beeftv-Owner", owner); code != http.StatusOK {
		t.Fatalf("owner credential should pass the session guard, got %d", code)
	}
	if code := call("X-Beeftv-Agent-Token", host); code != http.StatusOK {
		t.Fatalf("assistant host credential should pass the session guard, got %d", code)
	}
	if code := call("X-Beeftv-Agent-Token", "not-the-host-token"); code != http.StatusUnauthorized {
		t.Fatalf("wrong host token should be 401, got %d", code)
	}
}
