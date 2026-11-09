//go:build (darwin && cgo) || (linux && (amd64 || arm64))

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// TestNativeLifecycle creates an isolated OUTER test session. The plugin's
// invocation itself must inherit that session/group unchanged.
func TestNativeLifecycle(t *testing.T) {
	if os.Getenv("OLLAMA_LIFECYCLE_OUTER") != "1" {
		cmd := exec.Command(os.Args[0], "-test.run=^TestNativeLifecycle$", "-test.v")
		cmd.Env = append(os.Environ(), "OLLAMA_LIFECYCLE_OUTER=1")
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
		output, err := cmd.CombinedOutput()
		t.Log(string(output))
		if err != nil {
			t.Fatal(err)
		}
		return
	}
	for _, mode := range []string{"normal", "child", "orphan", "pipes", "resistant", "timeout", "nonzero", "reader_error", "blocked_stdin", "cleanup_denied", "heavy_output", "deadline_nonzero"} {
		t.Run(mode, func(t *testing.T) {
			dir := t.TempDir()
			pidFile := filepath.Join(dir, "child.pid")
			groupFile := filepath.Join(dir, "group")
			script := "#!/bin/sh\nps -o pgid= -p $$ > '" + groupFile + "'\n"
			if mode == "child" || mode == "orphan" || mode == "pipes" || mode == "resistant" || mode == "nonzero" || mode == "cleanup_denied" {
				child := "sleep 60"
				if mode == "resistant" {
					child = "trap '' TERM; exec sleep 60"
				}
				if mode == "orphan" {
					script += "(sh -c \"" + child + "\" & echo $! > '" + pidFile + "')\n"
				} else {
					script += "sh -c \"" + child + "\" &\necho $! > '" + pidFile + "'\n"
				}
				// Ensure the resistant child has installed its handler.
				script += "sleep 0.05\n"
			}
			switch mode {
			case "timeout", "blocked_stdin":
				script += "sleep 60\n"
			case "reader_error":
				script += "/usr/bin/yes x | /usr/bin/tr -d '\\n'\n"
			case "nonzero":
				script += "echo failed >&2\nexit 7\n"
			case "heavy_output":
				script += "i=0; while [ $i -lt 180 ]; do printf '\\033[32manswer\\033[0m\\n'; i=$((i+1)); done\n"
			case "deadline_nonzero":
				script += "echo answer\nexit 7\n"
			default:
				script += "printf '\\033[32manswer\\033[0m\\n'\n"
			}
			path := filepath.Join(dir, "ollama")
			if err := os.WriteFile(path, []byte(script), 0700); err != nil {
				t.Fatal(err)
			}
			// A preexisting Ollama-service analogue in the same group must survive.
			sentinel := exec.Command("sleep", "60")
			if err := sentinel.Start(); err != nil {
				t.Fatal(err)
			}
			defer func() { _ = sentinel.Process.Kill(); _ = sentinel.Wait() }()
			before, err := (nativeProcesses{}).read(sentinel.Process.Pid)
			if err != nil || before == nil {
				t.Fatal("sentinel identity unavailable", err)
			}
			prompt := "prompt"
			if mode == "blocked_stdin" {
				prompt = strings.Repeat("x", 1024*1024)
			}
			started := time.Now()
			callbacks := 0
			var native processObserver = nativeProcesses{}
			if mode == "cleanup_denied" {
				native = &deniedFinalization{}
			}
			stdout, stderr, cmdErr, timedOut, confirmed := executeOllama(path, "fake", prompt, time.Second, func(string, string) {
				callbacks++
				if mode == "heavy_output" {
					time.Sleep(3 * time.Millisecond)
				}
				if mode == "deadline_nonzero" {
					// Ensure timeout and exit polling are both ready after this
					// controlled callback. An already-exited child must win.
					time.Sleep(1100 * time.Millisecond)
				}
			}, native)
			if mode == "cleanup_denied" {
				data, err := os.ReadFile(pidFile)
				if err != nil {
					t.Fatal(err)
				}
				pid, _ := strconv.Atoi(strings.TrimSpace(string(data)))
				p, err := (nativeProcesses{}).read(pid)
				if err != nil || p == nil {
					t.Fatal("fixture identity unavailable", err)
				}
				// Test-owned teardown only; the denied observer must not signal it.
				wasLive := p.live
				_ = (nativeProcesses{}).send(*p, syscall.SIGKILL)
				if confirmed || !wasLive {
					t.Fatal("denied cleanup accepted or sent an unverified signal")
				}
				return
			}
			if time.Since(started) > 6*time.Second {
				t.Fatal("unbounded execution/finalization")
			}
			if mode == "reader_error" {
				if confirmed {
					t.Fatal("reader failure was accepted")
				}
			} else if !confirmed {
				t.Fatal("cleanup unverified", cmdErr)
			}
			if timedOut != (mode == "timeout" || mode == "blocked_stdin") {
				t.Fatalf("unexpected timeout %v", timedOut)
			}
			if mode == "nonzero" && (cmdErr == nil || stderr != "failed\n") {
				t.Fatal("lost nonzero exit or stderr", cmdErr, stderr)
			}
			if mode == "deadline_nonzero" {
				exit, ok := cmdErr.(*exec.ExitError)
				if !ok || exit.ExitCode() != 7 {
					t.Fatal("deadline overwrote nonzero error", cmdErr)
				}
			}
			if mode == "heavy_output" && stdout != strings.Repeat("answer\n", 180) {
				t.Fatal("queued output was truncated", len(stdout))
			}
			if mode != "timeout" && mode != "blocked_stdin" && mode != "reader_error" && mode != "nonzero" && mode != "heavy_output" && stdout != "answer\n" {
				t.Fatal("ANSI/output regression", stdout)
			}
			after, err := (nativeProcesses{}).read(before.pid)
			if err != nil || after == nil || !after.live || before.birth != after.birth {
				t.Fatal("preexisting service killed", err)
			}
			group, err := os.ReadFile(groupFile)
			if err != nil || strings.TrimSpace(string(group)) != strconv.Itoa(syscall.Getpgrp()) {
				t.Fatal("provider escaped inherited group", err, string(group))
			}
			if data, err := os.ReadFile(pidFile); err == nil {
				pid, _ := strconv.Atoi(strings.TrimSpace(string(data)))
				p, err := (nativeProcesses{}).read(pid)
				if err != nil || (p != nil && p.live) {
					t.Fatal("live descendant after result", pid, err)
				}
			}
			count := callbacks
			time.Sleep(25 * time.Millisecond)
			if callbacks != count {
				t.Fatal("late callback")
			}
		})
	}
}

type deniedFinalization struct {
	nativeProcesses
	snapshotted bool
}

func (n *deniedFinalization) members(pgid int) ([]processIdentity, error) {
	if n.snapshotted {
		return nil, fmt.Errorf("finalization observation denied")
	}
	n.snapshotted = true
	return n.nativeProcesses.members(pgid)
}

type fakeObserver struct {
	nativeProcesses
	items []processIdentity
	err   error
	sent  []int
}

func (f *fakeObserver) members(int) ([]processIdentity, error) { return f.items, f.err }
func (f *fakeObserver) send(p processIdentity, sig syscall.Signal) bool {
	f.sent = append(f.sent, p.pid)
	f.items = nil
	return true
}

func TestOwnershipSafety(t *testing.T) {
	f := &fakeObserver{items: []processIdentity{{pid: 10, birth: [2]uint64{1}, live: true}, {pid: 20, birth: [2]uint64{2}, live: true}}}
	l := &lifecycle{native: f, protected: map[int][2]uint64{10: {1}}, owned: map[int][2]uint64{20: {1}}, confirmed: true}
	if l.cleanup(func() {}) || len(f.sent) != 0 {
		t.Fatal("PID reuse/protected PID authorized a signal")
	}
	f.err = fmt.Errorf("observation denied")
	if l.cleanup(func() {}) {
		t.Fatal("observation failure was accepted")
	}
}

func TestNativeSignalVerification(t *testing.T) {
	n := nativeProcesses{}
	p, err := n.read(os.Getpid())
	if err != nil || p == nil {
		t.Fatal(err)
	}
	p.birth[0]++
	if n.send(*p, syscall.SIGKILL) {
		t.Fatal("mismatched birth accepted")
	}
	p, _ = n.read(os.Getpid())
	p.pgid++
	if n.send(*p, syscall.SIGKILL) {
		t.Fatal("mismatched group accepted")
	}
}

func TestLaunchFailure(t *testing.T) {
	_, _, err, timeout, confirmed := executeOllama("/nonexistent/ollama", "fake", "prompt", time.Second, func(string, string) {}, nativeProcesses{})
	if err == nil || timeout || !confirmed {
		t.Fatal("launch failure not finalized", err, timeout, confirmed)
	}
}

func TestObservationFailure(t *testing.T) {
	f := &fakeObserver{err: fmt.Errorf("enumeration denied")}
	callbacks := 0
	_, _, err, timeout, confirmed := executeOllama("/bin/false", "fake", "prompt", time.Second, func(string, string) { callbacks++ }, f)
	if err == nil || timeout || confirmed || callbacks != 0 || len(f.sent) != 0 {
		t.Fatal("failed observation authorized launch/output", err, timeout, confirmed)
	}
}

// TestAPPProtocol exercises the real main/APP path using the native test binary,
// without claiming that an installed standalone provider binary was validated.
func TestAPPProtocol(t *testing.T) {
	if os.Getenv("OLLAMA_APP_TEST") == "1" {
		main()
		os.Exit(0)
	}
	for _, mode := range []string{"success", "nonzero", "timeout", "reader_error"} {
		t.Run(mode, func(t *testing.T) {
			dir := t.TempDir()
			script := "#!/bin/sh\n"
			timeoutSeconds := 1
			switch mode {
			case "success":
				script += "cat >/dev/null\nprintf '\\033[32manswer\\033[0m\\n'\n"
			case "nonzero":
				script += "echo failed >&2\nexit 7\n"
			case "timeout":
				script += "sleep 60\n"
			case "reader_error":
				// Exceed the scanner's single-line bound with finite bytes rather
				// than racing a slow infinite pipeline against the timeout case.
				script += fmt.Sprintf("/usr/bin/head -c %d /dev/zero\n", maxScannerBufSize+1)
				timeoutSeconds = 5
			}
			if err := os.WriteFile(filepath.Join(dir, "ollama"), []byte(script), 0700); err != nil {
				t.Fatal(err)
			}
			cmd := exec.Command(os.Args[0], "-test.run=^TestAPPProtocol$")
			cmd.Env = append(os.Environ(), "OLLAMA_APP_TEST=1", "PATH="+dir+":"+os.Getenv("PATH"))
			cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
			cmd.Stdin = strings.NewReader("{\"type\":\"app.handshake.request.v1\"}\n" +
				fmt.Sprintf("{\"action\":\"generate\",\"invoke_id\":\"test\",\"input\":{\"model\":\"fake\",\"prompt\":\"test\",\"timeout_seconds\":%d}}\n", timeoutSeconds))
			output, err := cmd.Output()
			if err != nil {
				t.Fatal(err)
			}
			lines := strings.Split(strings.TrimSpace(string(output)), "\n")
			results := 0
			for i, line := range lines {
				var frame map[string]interface{}
				if err := json.Unmarshal([]byte(line), &frame); err != nil {
					t.Fatal("invalid APP frame", err)
				}
				if frame["type"] != "app.invoke.result.v1" {
					continue
				}
				results++
				if i != len(lines)-1 {
					t.Fatal("frames after final result")
				}
				if mode == "success" {
					if frame["status"] != "success" || frame["output"].(map[string]interface{})["response_text"] != "answer" {
						t.Fatal("success output regression", frame)
					}
				} else {
					code := map[string]string{"nonzero": "tool_execution_error", "timeout": "timeout_error", "reader_error": "adapter_internal_error"}[mode]
					if frame["status"] != "failed" || frame["error"].(map[string]interface{})["code"] != code {
						t.Fatal("incorrect failure protocol", frame)
					}
				}
			}
			if results != 1 {
				t.Fatal("missing/duplicate result")
			}
		})
	}
}
