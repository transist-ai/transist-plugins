//go:build darwin && cgo

package main

/*
#cgo LDFLAGS: -lproc
#include <libproc.h>
#include <sys/proc_info.h>
#include <errno.h>
#include <signal.h>
#include <stdlib.h>

// Export only selected metadata, never the native comm/name fields.
typedef struct { int pid, ppid, pgid, live, error; unsigned long long sec, usec; } identity;
static identity read_identity(int pid) {
    struct proc_bsdinfo info;
    identity out = {0};
    errno = 0;
    int size = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info));
    if (size != sizeof(info)) { out.error = errno ? errno : EIO; return out; }
    out.pid = pid; out.ppid = info.pbi_ppid; out.pgid = info.pbi_pgid;
    out.live = info.pbi_status != 5;
    out.sec = info.pbi_start_tvsec; out.usec = info.pbi_start_tvusec;
    return out;
}
static int ancestor_parent(int pid) {
    struct proc_bsdshortinfo info;
    if (proc_pidinfo(pid, PROC_PIDT_SHORTBSDINFO, 0, &info, sizeof(info)) != sizeof(info) || info.pbsi_pid != pid) return -1;
    return info.pbsi_ppid;
}
typedef struct { int *pids; int count, error; } pid_list;
static pid_list group_pids(int pgid) {
    pid_list out = {0};
    for (int capacity = 256; capacity <= 1048576; capacity *= 2) {
        int *pids = malloc(capacity * sizeof(int));
        if (!pids) { out.error = ENOMEM; return out; }
        int size = proc_listpids(PROC_PGRP_ONLY, pgid, pids, capacity * sizeof(int));
        if (size <= 0) { free(pids); out.error = EIO; return out; }
        if (size < capacity * sizeof(int)) { out.pids = pids; out.count = size / sizeof(int); return out; }
        free(pids);
    }
    out.error = EIO; return out;
}
static int list_pid(pid_list list, int i) { return list.pids[i]; }
static void free_list(pid_list list) { free(list.pids); }
*/
import "C"

import (
	"fmt"
	"syscall"
)

// Darwin's libproc ABI is accessed through small C wrappers to avoid unsafe Go
// imports and hand-coded struct offsets. cgo and Apple's SDK are build-only.
type nativeProcesses struct{}

func (nativeProcesses) read(pid int) (*processIdentity, error) {
	p := C.read_identity(C.int(pid))
	if p.error == C.ESRCH {
		return nil, nil
	}
	if p.error != 0 {
		return nil, syscall.Errno(p.error)
	}
	return &processIdentity{pid: int(p.pid), ppid: int(p.ppid), pgid: int(p.pgid), birth: [2]uint64{uint64(p.sec), uint64(p.usec)}, live: p.live != 0}, nil
}

func (n nativeProcesses) ancestor(pid int) (int, [2]uint64, error) {
	p, err := n.read(pid)
	if err == syscall.EPERM || err == syscall.EACCES {
		parent := C.ancestor_parent(C.int(pid))
		if parent >= 0 {
			return int(parent), [2]uint64{}, nil
		}
	}
	if err != nil {
		return 0, [2]uint64{}, err
	}
	if p == nil {
		return 0, [2]uint64{}, fmt.Errorf("ancestor disappeared")
	}
	return p.ppid, p.birth, nil
}

func (n nativeProcesses) members(pgid int) ([]processIdentity, error) {
	list := C.group_pids(C.int(pgid))
	if list.error != 0 {
		return nil, fmt.Errorf("group observation unavailable")
	}
	defer C.free_list(list)
	var result []processIdentity
	for i := 0; i < int(list.count); i++ {
		pid := int(C.list_pid(list, C.int(i)))
		if pid <= 0 {
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

// Darwin has no pidfd: reverify birth/group immediately before individual kill.
func (n nativeProcesses) send(expected processIdentity, sig syscall.Signal) bool {
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
	err = syscall.Kill(expected.pid, sig)
	return err == nil || err == syscall.ESRCH
}
