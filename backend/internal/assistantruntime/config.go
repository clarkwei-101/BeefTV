package assistantruntime

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const hostConfigFile = "agent_config.json"

// hostDirEnv 允许本机部署显式指定内置宿主目录。
// 打包形态（.app）由 BundledConfig 解析 Resources/agent-host；
// 本机开发形态没有随包运行时，需要靠这个变量或仓库内回退找到 agent-host/。
const hostDirEnv = "BEEFTV_AGENT_HOST_DIR"

// hostDiscoveryEnv 控制是否允许从工作目录向上自动发现仓库内的 agent-host/。
//
// 自动发现是本机开发形态的便利（`go run ./cmd/server` 时 CWD 在 backend/），
// 但它依赖 CWD，测试进程同样跑在仓库里，会让"本机没有宿主"的用例失真。
// 设为 "0"/"false"/"off" 时关闭自动发现，显式 BEEFTV_AGENT_HOST_DIR 仍然生效。
const hostDiscoveryEnv = "BEEFTV_AGENT_HOST_DISCOVERY"

// HostConfig 记录当前使用的文本模型与宿主启动命令；由本机用户在设置里配置。
type HostConfig struct {
	Model       string   `json:"model"`
	HostCommand string   `json:"hostCommand"`
	HostArgs    []string `json:"hostArgs,omitempty"`
	UpdatedAt   string   `json:"updatedAt,omitempty"`
}

func (h *Host) EffectiveConfig() (HostConfig, bool) {
	config, configured := readHostConfig(h.dataDir())
	if strings.TrimSpace(config.HostCommand) == "" {
		if bundled := BundledConfig(h.executablePath(), h.goos()); bundled.HostCommand != "" {
			config.HostCommand = bundled.HostCommand
			config.HostArgs = bundled.HostArgs
			configured = true
		}
	}
	if strings.TrimSpace(config.HostCommand) == "" {
		if local := LocalHostConfig(); local.HostCommand != "" {
			config.HostCommand = local.HostCommand
			config.HostArgs = local.HostArgs
			configured = true
		}
	}
	return config, configured
}

func (h *Host) WriteConfig(config HostConfig) error {
	return writeHostConfig(h.dataDir(), config)
}

func readHostConfig(dataDir string) (HostConfig, bool) {
	raw, err := os.ReadFile(filepath.Join(dataDir, hostConfigFile))
	if err != nil {
		return HostConfig{}, false
	}
	var config HostConfig
	if json.Unmarshal(raw, &config) != nil {
		return HostConfig{}, false
	}
	return config, true
}

func writeHostConfig(dataDir string, config HostConfig) error {
	encoded, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dataDir, ".agent-config-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(encoded); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, filepath.Join(dataDir, hostConfigFile))
}

// BundledConfig 从可执行文件布局解析随包 Node 运行时。
// 桌面两种布局都直接执行 Node；Windows 不能依赖 POSIX shell。
func BundledConfig(executable, goos string) HostConfig {
	if strings.TrimSpace(executable) == "" {
		return HostConfig{}
	}
	root := filepath.Join(filepath.Dir(executable), "..", "Resources", "agent-host")
	node := filepath.Join(root, "runtime", "bin", "node")
	if goos == "windows" {
		root = filepath.Join(filepath.Dir(executable), "agent-host")
		node = filepath.Join(root, "runtime", "node.exe")
	}
	entry := filepath.Join(root, "server.mjs")
	for _, file := range []string{node, entry} {
		if info, err := os.Stat(file); err != nil || info.IsDir() {
			return HostConfig{}
		}
	}
	return HostConfig{HostCommand: node, HostArgs: []string{entry}}
}

// LocalHostConfig 解析本机（非打包）部署的内置宿主。
//
// 打包形态由 BundledConfig 负责；本机开发/本机 Web 形态没有 .app 布局，
// 宿主目录来自 BEEFTV_AGENT_HOST_DIR，或从当前工作目录向上找仓库里的
// agent-host/。找不到时返回空配置，调用方按"未配置宿主"处理。
func LocalHostConfig() HostConfig {
	for _, dir := range localHostDirCandidates() {
		if config := hostConfigFromDir(dir); config.HostCommand != "" {
			return config
		}
	}
	return HostConfig{}
}

// localHostDirCandidates 列出本机部署可能使用��宿主目录，按优先级排序：
// 显式环境变量优先，然后是从当前工作目录逐级向上找到的仓库内 agent-host/。
//
// 向上查找必须同时满足「有 agent-host/」和「看起来像仓库根」两个条件。
// 只看 agent-host/ 会在任何父目录里命中同名目录（包括测试运行时的仓库
// 本身），把宿主指到一个与当前后端无关的副本上。
func localHostDirCandidates() []string {
	var dirs []string
	if explicit := strings.TrimSpace(os.Getenv(hostDirEnv)); explicit != "" {
		dirs = append(dirs, explicit)
	}
	if !hostDiscoveryEnabled() {
		return dirs
	}
	wd, err := os.Getwd()
	if err != nil {
		return dirs
	}
	// 从当前工作目录逐级向上找 agent-host/，覆盖 backend/ 与仓库根两种启动位置。
	for dir := wd; ; {
		candidate := filepath.Join(dir, "agent-host")
		if _, err := os.Stat(candidate); err == nil && isRepositoryRoot(dir) {
			dirs = append(dirs, candidate)
			break
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return dirs
}

// hostDiscoveryEnabled 报告是否允许基于工作目录的自动发现。
// 只有显式关闭（0/false/off/no）时才禁用，其余取值都保持开启。
func hostDiscoveryEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(hostDiscoveryEnv))) {
	case "0", "false", "off", "no":
		return false
	default:
		return true
	}
}

// isRepositoryRoot 判断一个目录是否是本仓库根。
// 仓库根至少会有 backend/ 与 web/ 两个子目录之一，加上 agent-host/。
func isRepositoryRoot(dir string) bool {
	for _, marker := range []string{"backend", "web", "AGENTS.md"} {
		if _, err := os.Stat(filepath.Join(dir, marker)); err == nil {
			return true
		}
	}
	return false
}

// hostConfigFromDir 在给定宿主目录里解析启动命令：
// 优先用随包的 runtime/bin/node，没有随包运行时时回退到 PATH 上的 node。
func hostConfigFromDir(dir string) HostConfig {
	entry := filepath.Join(dir, "server.mjs")
	if info, err := os.Stat(entry); err != nil || info.IsDir() {
		return HostConfig{}
	}
	bundled := filepath.Join(dir, "runtime", "bin", "node")
	if info, err := os.Stat(bundled); err == nil && !info.IsDir() {
		return HostConfig{HostCommand: bundled, HostArgs: []string{entry}}
	}
	node, err := exec.LookPath("node")
	if err != nil || strings.TrimSpace(node) == "" {
		return HostConfig{}
	}
	return HostConfig{HostCommand: node, HostArgs: []string{entry}}
}
