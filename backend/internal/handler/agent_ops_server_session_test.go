package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"infinite-canvas/backend/internal/agentops"
	"infinite-canvas/backend/internal/model"
)

// 公网回归：经隧道进来的浏览器 Origin 与后端 Host 不一致，
// 已登录会话（ServerAuthMiddleware 注入）必须能走 /api/ops 写画布；
// 未登录的公网请求仍 403。
func TestServerSessionCanCommitThroughPublicOrigin(t *testing.T) {
	env := newAssistantTestEnv(t, nil)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		c.Set(authSessionKey, &model.Account{ID: "tester", Username: "tester"})
	})
	router.Use(RuntimeDependenciesMiddleware(RuntimeDependencies{}))
	api := router.Group("/api")
	RegisterAgentOpsRoutes(api, env.service, agentops.NewStore(env.service.Database()), env.clients)

	postPublic := func(opID string, withSession bool) (int, string) {
		body := `{"opId":"` + opID + `","params":{"canvasId":"` + env.canvasID +
			`","expectedRevision":1,"document":{"title":"公网提交","nodes":[{"id":"n1","type":"text","title":"公网"}],"connections":[]}}}`
		req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:18090/api/ops/canvas.document.commit", strings.NewReader(body))
		// 模拟公网：Host 是后端监听地址，Origin 是隧道域名，RemoteAddr 非 loopback。
		req.Host = "127.0.0.1:18090"
		req.RemoteAddr = "10.0.0.9:5555"
		req.Header.Set("Origin", "https://nearest-christ-brokers-fiscal.trycloudflare.com")
		req.Header.Set("Content-Type", "application/json")
		if !withSession {
			// 去掉会话：复刻未登录公网请求。
			clean := gin.New()
			clean.Use(RuntimeDependenciesMiddleware(RuntimeDependencies{}))
			cleanAPI := clean.Group("/api")
			RegisterAgentOpsRoutes(cleanAPI, env.service, agentops.NewStore(env.service.Database()), env.clients)
			rec := httptest.NewRecorder()
			clean.ServeHTTP(rec, req)
			return rec.Code, rec.Body.String()
		}
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		return rec.Code, rec.Body.String()
	}

	if code, body := postPublic("public-session-1", false); code != http.StatusForbidden {
		t.Fatalf("未登录公网请求应 403: %d %s", code, body)
	}
	if code, body := postPublic("public-session-2", true); code != http.StatusOK {
		t.Fatalf("已登录会话公网提交失败: %d %s", code, body)
	}
}
