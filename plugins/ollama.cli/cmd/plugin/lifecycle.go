package main

import (
	"fmt"
	"os"
	"syscall"
	"time"
)

// processIdentity contains native metadata only, never command lines or env.
type processIdentity struct {
	pid, ppid, pgid int
	birth           [2]uint64
	live            bool
}

type processObserver interface {
	read(int) (*processIdentity, error)
	members(int) ([]processIdentity, error)
	ancestor(int) (int, [2]uint64, error)
	send(processIdentity, syscall.Signal) bool
}

type lifecycle struct {
	native    processObserver
	pgid      int
	protected map[int][2]uint64
	owned     map[int][2]uint64
	confirmed bool
}

// newLifecycle snapshots protected PIDs before launching into the runtime group.
func newLifecycle(native processObserver) (*lifecycle, error) {
	l := &lifecycle{native: native, pgid: syscall.Getpgrp(), protected: make(map[int][2]uint64), owned: make(map[int][2]uint64), confirmed: true}
	for pid := os.Getpid(); pid != 0; {
		if _, exists := l.protected[pid]; exists {
			return nil, fmt.Errorf("ancestor observation incomplete")
		}
		if pid == 1 {
			l.protected[pid] = [2]uint64{}
			break
		}
		parent, birth, err := native.ancestor(pid)
		if err != nil {
			return nil, err
		}
		l.protected[pid] = birth
		pid = parent
	}
	members, err := native.members(l.pgid)
	if err != nil {
		return nil, err
	}
	for _, p := range members {
		l.protected[p.pid] = p.birth
	}
	return l, nil
}

// remaining discovers orphans too, using inherited group membership as authority.
func (l *lifecycle) remaining() ([]processIdentity, error) {
	members, err := l.native.members(l.pgid)
	if err != nil {
		return nil, err
	}
	var result []processIdentity
	for _, p := range members {
		if birth, ok := l.protected[p.pid]; ok {
			if birth != ([2]uint64{}) && birth != p.birth {
				l.confirmed = false
			}
			continue
		}
		if birth, ok := l.owned[p.pid]; ok && birth != p.birth {
			l.confirmed = false
			continue
		}
		l.owned[p.pid] = p.birth
		if p.live {
			result = append(result, p)
		}
	}
	return result, nil
}

// cleanup uses one four-second deadline, TERM then KILL, never killpg.
func (l *lifecycle) cleanup(tick func()) bool {
	deadline := time.Now().Add(4 * time.Second)
	termDeadline := deadline.Add(-2 * time.Second)
	for _, sig := range []syscall.Signal{syscall.SIGTERM, syscall.SIGKILL} {
		phaseDeadline := deadline
		if sig == syscall.SIGTERM {
			phaseDeadline = termDeadline
		}
		sent := make(map[processIdentity]bool)
		for {
			members, err := l.remaining()
			if err != nil {
				return false
			}
			if len(members) == 0 {
				return l.confirmed
			}
			for _, p := range members {
				if !sent[p] {
					if !l.native.send(p, sig) {
						l.confirmed = false
					}
					sent[p] = true
				}
			}
			tick()
			left := time.Until(phaseDeadline)
			if left <= 0 {
				break
			}
			if left > 25*time.Millisecond {
				left = 25 * time.Millisecond
			}
			time.Sleep(left)
		}
	}
	members, err := l.remaining()
	return err == nil && len(members) == 0 && l.confirmed
}
