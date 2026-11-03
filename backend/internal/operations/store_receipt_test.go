package operations_test

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"infinite-canvas/backend/internal/database"
	"infinite-canvas/backend/internal/model"
	"infinite-canvas/backend/internal/operations"
)

// countingLogger 统计 GORM 记下的错误条数，用来断言「已处理的重复键不再进日志」。
type countingLogger struct {
	logger.Interface
	mu     sync.Mutex
	errors int
}

func (l *countingLogger) Error(ctx context.Context, s string, args ...interface{}) {
	l.mu.Lock()
	l.errors++
	l.mu.Unlock()
	l.Interface.Error(ctx, s, args...)
}

func (l *countingLogger) count() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.errors
}

func newReceiptStore(t *testing.T) (*operations.Store, *gorm.DB, *countingLogger) {
	t.Helper()
	counter := &countingLogger{Interface: logger.Default.LogMode(logger.Silent)}
	db, err := gorm.Open(sqlite.Open(filepath.Join(t.TempDir(), "receipts.db")),
		&gorm.Config{Logger: counter})
	if err != nil {
		t.Fatal(err)
	}
	sqlDB, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	sqlDB.SetMaxOpenConns(1)
	sqlDB.SetMaxIdleConns(1)
	if err := database.MigrateLocalSchema(db); err != nil {
		t.Fatal(err)
	}
	return operations.NewStore(db), db, counter
}

// 回放路径不能再写库：恢复任务每轮都会重扫已成功的操作，如果每次都先撞一次
// 唯一键，GORM 会把这条已被接住的错误按 ERROR 级别写进日志，几小时就能写出几十 MB。
func TestStoreReplaysReceiptWithoutDuplicateInsert(t *testing.T) {
	store, db, counter := newReceiptStore(t)
	req := operations.RunRequest{
		UserID: "user-1", OpID: "attach-node:task-1:node-1:0", Op: "canvas.task.bind",
		PayloadHash: operations.PayloadHash("canvas.task.bind", []byte(`{"canvasId":"c1"}`)),
	}
	calls := 0
	run := func(tx *gorm.DB) ([]byte, error) {
		calls++
		return []byte(`{"ok":true}`), nil
	}
	first, err := store.Run(context.Background(), req, run)
	if err != nil {
		t.Fatal(err)
	}
	if first.Replayed || calls != 1 {
		t.Fatalf("first run = %#v calls=%d", first, calls)
	}
	errorsBefore := counter.count()
	second, err := store.Run(context.Background(), req, run)
	if err != nil {
		t.Fatalf("replay failed: %v", err)
	}
	if !second.Replayed {
		t.Fatalf("second run must replay, got %#v", second)
	}
	if calls != 1 {
		t.Fatalf("replay re-executed the write: calls=%d", calls)
	}
	if got := counter.count(); got != errorsBefore {
		t.Fatalf("replay logged %d database errors; replay must be silent", got-errorsBefore)
	}
	var count int64
	if err := db.Model(&model.AgentOpRecord{}).Where("user_id = ? AND op_id = ?", req.UserID, req.OpID).Count(&count).Error; err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("receipt rows = %d, want 1", count)
	}
}

// 上一轮崩溃留下的 running 回执不能永久挡住同一个 opId。
func TestStoreTakesOverStaleRunningReceipt(t *testing.T) {
	store, db, _ := newReceiptStore(t)
	req := operations.RunRequest{
		UserID: "user-1", OpID: "attach-node:task-2:node-2:0", Op: "canvas.task.bind",
		PayloadHash: operations.PayloadHash("canvas.task.bind", []byte(`{"canvasId":"c1"}`)),
	}
	stale := model.AgentOpRecord{UserID: req.UserID, OpID: req.OpID, Op: req.Op,
		PayloadHash: req.PayloadHash, Status: "running",
		CreatedAt: time.Now().Add(-30 * time.Minute), UpdatedAt: time.Now().Add(-30 * time.Minute)}
	if err := db.Create(&stale).Error; err != nil {
		t.Fatal(err)
	}
	outcome, err := store.Run(context.Background(), req, func(tx *gorm.DB) ([]byte, error) {
		return []byte(`{"ok":true}`), nil
	})
	if err != nil {
		t.Fatalf("stale receipt was not taken over: %v", err)
	}
	if outcome.Replayed {
		t.Fatalf("stale receipt must be re-executed, got %#v", outcome)
	}
	var record model.AgentOpRecord
	if err := db.Where("user_id = ? AND op_id = ?", req.UserID, req.OpID).First(&record).Error; err != nil {
		t.Fatal(err)
	}
	if record.Status != "succeeded" {
		t.Fatalf("receipt status = %q, want succeeded", record.Status)
	}
}

// 刚写下的 running 回执仍在执行中，不能被别人抢走。
func TestStoreKeepsFreshRunningReceiptBusy(t *testing.T) {
	store, db, _ := newReceiptStore(t)
	req := operations.RunRequest{
		UserID: "user-1", OpID: "attach-node:task-3:node-3:0", Op: "canvas.task.bind",
		PayloadHash: operations.PayloadHash("canvas.task.bind", []byte(`{"canvasId":"c1"}`)),
	}
	fresh := model.AgentOpRecord{UserID: req.UserID, OpID: req.OpID, Op: req.Op,
		PayloadHash: req.PayloadHash, Status: "running", CreatedAt: time.Now(), UpdatedAt: time.Now()}
	if err := db.Create(&fresh).Error; err != nil {
		t.Fatal(err)
	}
	called := false
	_, err := store.Run(context.Background(), req, func(tx *gorm.DB) ([]byte, error) {
		called = true
		return []byte(`{"ok":true}`), nil
	})
	if err == nil {
		t.Fatal("fresh running receipt must stay busy")
	}
	var opErr *operations.Error
	if !errors.As(err, &opErr) || opErr.Reason != "operation_in_progress" {
		t.Fatalf("unexpected error: %v", err)
	}
	if called {
		t.Fatal("fresh running receipt was executed concurrently")
	}
}
