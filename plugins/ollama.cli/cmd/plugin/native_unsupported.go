//go:build (darwin && !cgo) || (linux && !amd64 && !arm64) || (!darwin && !linux)

package main

import (
	"fmt"
	"syscall"
)

type nativeProcesses struct{}

func (nativeProcesses) read(int) (*processIdentity, error) {
	return nil, fmt.Errorf("native lifecycle observation unavailable")
}
func (nativeProcesses) members(int) ([]processIdentity, error) {
	return nil, fmt.Errorf("native lifecycle observation unavailable")
}
func (nativeProcesses) ancestor(int) (int, [2]uint64, error) {
	return 0, [2]uint64{}, fmt.Errorf("native lifecycle observation unavailable")
}
func (nativeProcesses) send(processIdentity, syscall.Signal) bool { return false }
