//go:build linux && (amd64 || arm64)

package main

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"syscall"
)

type nativeProcesses struct{}

// read parses only birth, parent, group and liveness from procfs stat.
func (nativeProcesses) read(pid int) (*processIdentity, error) {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	end := strings.LastIndexByte(string(data), ')')
	if end < 0 {
		return nil, fmt.Errorf("invalid native metadata")
	}
	fields := strings.Fields(string(data[end+1:]))
	if len(fields) < 20 {
		return nil, fmt.Errorf("incomplete native metadata")
	}
	parent, e1 := strconv.Atoi(fields[1])
	group, e2 := strconv.Atoi(fields[2])
	birth, e3 := strconv.ParseUint(fields[19], 10, 64)
	if e1 != nil || e2 != nil || e3 != nil {
		return nil, fmt.Errorf("invalid native identity")
	}
	return &processIdentity{pid: pid, ppid: parent, pgid: group, birth: [2]uint64{birth}, live: fields[0] != "Z" && fields[0] != "X"}, nil
}

func (n nativeProcesses) ancestor(pid int) (int, [2]uint64, error) {
	p, err := n.read(pid)
	if err == nil && p == nil {
		err = fmt.Errorf("ancestor disappeared")
	}
	if err != nil {
		return 0, [2]uint64{}, err
	}
	return p.ppid, p.birth, nil
}

func (n nativeProcesses) members(pgid int) ([]processIdentity, error) {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil, err
	}
	var result []processIdentity
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil {
			continue
		}
		p, err := n.read(pid)
		if err != nil {
			return nil, err
		}
		if p != nil && p.pgid == pgid {
			result = append(result, *p)
		}
	}
	return result, nil
}

// send pins a PID before rechecking birth/group, preventing PID-reuse kills.
// pidfd syscall numbers are identical on Linux amd64 and arm64; older kernels
// fail closed. No unsafe pointers or third-party dependencies are needed.
func (n nativeProcesses) send(expected processIdentity, sig syscall.Signal) bool {
	fd, _, errno := syscall.Syscall(434, uintptr(expected.pid), 0, 0)
	if errno == syscall.ESRCH {
		return true
	}
	if errno != 0 {
		return false
	}
	defer syscall.Close(int(fd))
	p, err := n.read(expected.pid)
	if err != nil {
		return false
	}
	if p == nil {
		return true
	}
	if p.birth != expected.birth || p.pgid != expected.pgid {
		return false
	}
	if !p.live {
		return true
	}
	_, _, errno = syscall.Syscall6(424, fd, uintptr(sig), 0, 0, 0, 0)
	return errno == 0 || errno == syscall.ESRCH
}
