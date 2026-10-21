package handler

import (
	"os"
	"testing"
)

// TestMain 关闭宿主目录的自动发现。
//
// 本机开发形态下后端会从工作目录向上找到仓库里的 agent-host/，但测试进程同样
// 跑在仓库里；不关掉的话，"本机没有配置宿主"的用例会意外启动真实宿主，
// 断言的其实是另一套状态。显式写入的 host/config 与 BEEFTV_AGENT_HOST_DIR 不受影响。
func TestMain(m *testing.M) {
	_ = os.Setenv("BEEFTV_AGENT_HOST_DISCOVERY", "0")
	os.Exit(m.Run())
}
