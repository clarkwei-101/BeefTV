package handler

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"infinite-canvas/backend/internal/app"
	"infinite-canvas/backend/internal/database"
	"infinite-canvas/backend/internal/repository"
	"infinite-canvas/backend/internal/workspace"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

// This catches regressions where request ownership silently falls back to a
// cookie-derived user instead of the desktop workspace injected at startup.
func TestWorkspaceMiddlewareInjectsStableContext(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.Use(WorkspaceMiddleware(workspace.Context{ID: "default", DataDir: "/tmp/canvas"}))
	router.GET("/probe", func(c *gin.Context) {
		scope, err := CurrentWorkspace(c)
		if err != nil {
			c.String(http.StatusInternalServerError, err.Error())
			return
		}
		c.String(http.StatusOK, scope.ID+"|"+scope.DataDir)
	})

	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/probe", nil))

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", response.Code, http.StatusOK, response.Body.String())
	}
	if response.Body.String() != "default|/tmp/canvas" {
		t.Fatalf("workspace = %q, want stable injected scope", response.Body.String())
	}
}

// This catches regressions where business routes still require a session
// cookie after a local workspace has been initialized.
func TestCurrentUserResolvesWorkspaceOwnerWithoutCookie(t *testing.T) {
	gin.SetMode(gin.TestMode)
	db, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if err := database.MigrateLocalSchema(db); err != nil {
		t.Fatal(err)
	}
	svc := app.New(repository.New(db), t.TempDir())
	owner, err := svc.LocalWorkspaceOwner()
	if err != nil {
		t.Fatal(err)
	}

	router := gin.New()
	router.Use(WorkspaceMiddleware(workspace.Context{ID: owner.ID, DataDir: t.TempDir()}))
	router.GET("/probe", func(c *gin.Context) {
		user, resolveErr := currentUser(c, svc)
		if resolveErr != nil {
			failService(c, resolveErr)
			return
		}
		c.String(http.StatusOK, user.Username)
	})
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/probe", nil))

	if response.Code != http.StatusOK || response.Body.String() != "local" {
		t.Fatalf("status = %d, body = %q; want cookie-free local owner", response.Code, response.Body.String())
	}
	// 公网账号体系（v14 迁移）要求 auth_sessions 表存在：它是登录会话存储，
	// 与本地资源归属的 User 投影互不干扰。cookie-free 断言已在上面覆盖。
	if !db.Migrator().HasTable("auth_sessions") {
		t.Fatal("auth_sessions table missing: public account sessions have nowhere to persist")
	}
}

func TestCurrentUserResolvesLocalOwnerWithoutWorkspaceMiddleware(t *testing.T) {
	gin.SetMode(gin.TestMode)
	db, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if err := database.MigrateLocalSchema(db); err != nil {
		t.Fatal(err)
	}
	svc := app.NewLocal(repository.New(db), t.TempDir())
	router := gin.New()
	router.GET("/probe", func(c *gin.Context) {
		user, resolveErr := currentUser(c, svc)
		if resolveErr != nil {
			failService(c, resolveErr)
			return
		}
		c.String(http.StatusOK, user.Username)
	})
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/probe", nil))
	if response.Code != http.StatusOK || response.Body.String() != "local" {
		t.Fatalf("status = %d, body = %q; want cookie-free local owner", response.Code, response.Body.String())
	}
}
