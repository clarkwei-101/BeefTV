package assistantruntime

import (
	"os"
	"path/filepath"
	"testing"
)

// chdirTo moves into dir for the duration of the test and returns a restore
// function. The path is resolved through symlinks because os.Getwd reports the
// physical path (on macOS /var is a symlink to /private/var), and the host
// discovery walks the physical tree.
func chdirTo(t *testing.T, dir string) func() {
	t.Helper()
	resolved, err := filepath.EvalSymlinks(dir)
	if err != nil {
		t.Fatal(err)
	}
	previous, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(resolved); err != nil {
		t.Fatal(err)
	}
	return func() { _ = os.Chdir(previous) }
}

// samePath compares two paths after resolving symlinks, because os.Getwd
// reports the physical path on macOS (/var is a symlink to /private/var).
func samePath(a, b string) bool {
	resolvedA, errA := filepath.EvalSymlinks(a)
	resolvedB, errB := filepath.EvalSymlinks(b)
	if errA == nil {
		a = resolvedA
	}
	if errB == nil {
		b = resolvedB
	}
	return a == b
}

// makeRepoRoot builds a directory that looks like the repository root: it has
// an agent-host/ entry and at least one repository marker.
func makeRepoRoot(t *testing.T) (root string, hostDir string, entry string) {
	t.Helper()
	root = t.TempDir()
	hostDir = filepath.Join(root, "agent-host")
	if err := os.MkdirAll(hostDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "backend"), 0o755); err != nil {
		t.Fatal(err)
	}
	entry = filepath.Join(hostDir, "server.mjs")
	if err := os.WriteFile(entry, []byte("fixture"), 0o755); err != nil {
		t.Fatal(err)
	}
	return root, hostDir, entry
}

// 本机（非打包）部署没有 .app 布局时，宿主目录必须能从环境变量或仓库里的
// agent-host/ 找到，否则内置创作助手永远无法启动，/assistant/status 只会
// 一直返回 host_start_failed。
func TestLocalHostConfigResolvesExplicitDirectory(t *testing.T) {
	hostDir := t.TempDir()
	entry := filepath.Join(hostDir, "server.mjs")
	if err := os.WriteFile(entry, []byte("fixture"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv(hostDirEnv, hostDir)
	// 隔离仓库回退：这个测试只关心显式目录。
	restore := chdirTo(t, t.TempDir())
	defer restore()

	got := LocalHostConfig()
	if got.HostCommand == "" {
		t.Fatal("explicit host dir must resolve a launch command")
	}
	if len(got.HostArgs) != 1 || got.HostArgs[0] != entry {
		t.Fatalf("host args must point at server.mjs: %#v", got)
	}
}

// 显式指定的目录里没有 server.mjs 时不能启动一个不存在的入口。
func TestLocalHostConfigIgnoresDirectoryWithoutEntry(t *testing.T) {
	hostDir := t.TempDir()
	t.Setenv(hostDirEnv, hostDir)
	// 隔离仓库回退：这个测试只关心显式目录本身不可用。
	restore := chdirTo(t, t.TempDir())
	defer restore()

	if got := LocalHostConfig(); got.HostCommand != "" {
		t.Fatalf("directory without server.mjs must not resolve: %#v", got)
	}
}

// 从当前工作目录逐级向上找 agent-host/，覆盖 backend/ 与仓库根两种启动位置。
func TestLocalHostConfigFindsRepositoryAgentHost(t *testing.T) {
	root, _, entry := makeRepoRoot(t)
	// 从仓库下的 backend/ 启动：向上两级才到 agent-host/。
	workDir := filepath.Join(root, "backend")
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		t.Fatal(err)
	}
	restore := chdirTo(t, workDir)
	defer restore()
	// 显式变量不能遮蔽仓库回退。
	t.Setenv(hostDirEnv, "")

	got := LocalHostConfig()
	if got.HostCommand == "" {
		t.Fatal("repository agent-host must be discovered from a nested working directory")
	}
	if len(got.HostArgs) != 1 || !samePath(got.HostArgs[0], entry) {
		t.Fatalf("host args must point at server.mjs: %#v", got)
	}
}

// 显式变量优先于仓库回退。
func TestLocalHostConfigPrefersExplicitDirectory(t *testing.T) {
	root, _, _ := makeRepoRoot(t)
	explicit := t.TempDir()
	explicitEntry := filepath.Join(explicit, "server.mjs")
	if err := os.WriteFile(explicitEntry, []byte("fixture"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv(hostDirEnv, explicit)
	restore := chdirTo(t, root)
	defer restore()

	got := LocalHostConfig()
	if len(got.HostArgs) != 1 || got.HostArgs[0] != explicitEntry {
		t.Fatalf("explicit host dir must win: %#v", got)
	}
}

// 只有 agent-host/ 而没有仓库标记的祖先目录不能被当成仓库根，
// 否则会指向一个与当前后端无关的宿主副本。
func TestLocalHostConfigIgnoresUnmarkedAncestorDirectory(t *testing.T) {
	root := t.TempDir()
	hostDir := filepath.Join(root, "agent-host")
	if err := os.MkdirAll(hostDir, 0o755); err != nil {
		t.Fatal(err)
	}
	entry := filepath.Join(hostDir, "server.mjs")
	if err := os.WriteFile(entry, []byte("fixture"), 0o755); err != nil {
		t.Fatal(err)
	}
	// 这个祖先目录只有 agent-host/，没有 backend/、web/ 或 AGENTS.md，
	// 因此不能被当成仓库根。
	workDir := filepath.Join(root, "nested")
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		t.Fatal(err)
	}
	restore := chdirTo(t, workDir)
	defer restore()
	t.Setenv(hostDirEnv, "")

	if got := LocalHostConfig(); got.HostCommand != "" {
		t.Fatalf("unmarked ancestor must not resolve a host: %#v", got)
	}
}
