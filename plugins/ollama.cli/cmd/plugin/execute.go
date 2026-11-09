package main

import (
	"bufio"
	"bytes"
	"io"
	"os"
	"os/exec"
	"time"
)

type pipeEvent struct {
	stream, line string
	err          error
	finished     bool
}

// executeOllama owns launch, readers, reaping and finalization. Only this calling
// goroutine mutates output or calls progress; workers never emit APP frames.
func executeOllama(path, model, prompt string, timeout time.Duration, progress func(string, string), native processObserver, limits ...int) (stdout, stderr string, cmdErr error, timedOut, confirmed bool) {
	limit := defaultCaptureBytes
	if len(limits) > 0 {
		limit = limits[0]
	}
	captured := 0
	exceeded := false
	l, err := newLifecycle(native)
	if err != nil {
		return "", "", err, false, false
	}
	var outBuf, errBuf bytes.Buffer
	events := make(chan pipeEvent, 64)
	stop := make(chan struct{})
	var readers []*os.File
	var writers []*os.File
	readerDone := make(chan struct{}, 2)
	startedReaders := 0
	finishedReaders := 0
	readerOK := true
	var cmd *exec.Cmd
	var stdinReader, stdinWriter *os.File
	stdinDone := make(chan struct{}, 1)
	started := false
	consume := func(e pipeEvent) {
		if e.finished {
			finishedReaders++
			if e.err != nil {
				readerOK = false
			}
			return
		}
		line := stripANSI(e.line)
		if exceeded {
			return
		}
		captured += len(e.line) + 1
		if captured > limit {
			remaining := max(0, limit-(captured-len(e.line)-1))
			prefix := utf8Prefix(line, min(remaining, len(line)))
			if e.stream == "stdout" {
				outBuf.WriteString(prefix)
			} else {
				errBuf.WriteString(prefix)
			}
			exceeded = true
			return
		}
		if e.stream == "stdout" {
			outBuf.WriteString(line + "\n")
		} else if line != "" {
			errBuf.WriteString(line + "\n")
		}
		progress(e.stream, line)
	}
	tick := func() {
		// A finite batch keeps continuous output from starving cleanup deadlines.
		for i := 0; i < cap(events); i++ {
			select {
			case e := <-events:
				consume(e)
			default:
				return
			}
		}
	}
	defer func() {
		if stdinWriter != nil {
			_ = stdinWriter.Close() // Interrupt the owned prompt writer, too.
		}
		if stdinReader != nil {
			_ = stdinReader.Close()
		}
		for _, f := range writers {
			_ = f.Close()
		}
		confirmed = true
		if started {
			confirmed = l.cleanup(tick)
		}
		// Readers and direct-child reap share one total second after cleanup.
		deadline := time.Now().Add(time.Second)
		// Spend the remaining budget draining EOF, reserving just one poll tick
		// for interrupting/joining workers on a held pipe. Heavy queued output
		// must not be truncated by an arbitrary 50ms EOF window.
		drainTimer := time.NewTimer(max(time.Until(deadline)-25*time.Millisecond, 0))
	drain:
		for finishedReaders < startedReaders {
			select {
			case e := <-events:
				consume(e)
			case <-drainTimer.C:
				break drain
			}
		}
		drainTimer.Stop()
		close(stop)
		if finishedReaders < startedReaders {
			readerOK = false // Interrupted/truncated output cannot be accepted.
		}
		for _, f := range readers {
			_ = f.Close() // os.Pipe uses Go's poller; close interrupts blocked reads.
		}
		for i := 0; i < startedReaders; i++ {
			select {
			case <-readerDone:
			case <-time.After(max(time.Until(deadline), 0)):
				confirmed = false
			}
		}
		tick()
		if started {
			select {
			case <-stdinDone:
			case <-time.After(max(time.Until(deadline), 0)):
				confirmed = false
			}
			// Never start an uncancellable OS wait worker. A direct child cannot
			// have its PID reused before reaping; native exit proof makes Wait
			// immediate, with no exec-managed I/O goroutines (all stdio is files).
			p, err := native.read(cmd.Process.Pid)
			if err == nil && (p == nil || !p.live) {
				cmdErr = cmd.Wait()
			} else {
				confirmed = false
				_ = cmd.Process.Release()
			}
		}
		confirmed = confirmed && readerOK
		if exceeded {
			cmdErr = errCaptureLimit
			timedOut = false
		}
		stdout, stderr = outBuf.String(), errBuf.String()
	}()
	cmd = exec.Command(path, "run", model)
	stdinReader, stdinWriter, err = os.Pipe()
	if err != nil {
		readerOK = false
		return "", "", err, false, false
	}
	cmd.Stdin = stdinReader
	for _, stream := range []string{"stdout", "stderr"} {
		r, w, err := os.Pipe()
		if err != nil {
			readerOK = false
			return "", "", err, false, false
		}
		readers = append(readers, r)
		writers = append(writers, w)
		if stream == "stdout" {
			cmd.Stdout = w
		} else {
			cmd.Stderr = w
		}
	}
	if err := cmd.Start(); err != nil {
		return "", "", err, false, true
	}
	started = true
	_ = stdinReader.Close()
	go func() {
		_, _ = io.WriteString(stdinWriter, prompt)
		_ = stdinWriter.Close()
		stdinDone <- struct{}{}
	}()
	for _, f := range writers {
		_ = f.Close()
	}
	for i, stream := range []string{"stdout", "stderr"} {
		startedReaders++
		go func(r *os.File, stream string) {
			defer func() { readerDone <- struct{}{} }()
			sc := bufio.NewScanner(r)
			sc.Buffer(make([]byte, 4096), maxScannerBufSize)
			for sc.Scan() {
				select {
				case events <- pipeEvent{stream: stream, line: sc.Text()}:
				case <-stop:
					return
				}
			}
			select {
			case events <- pipeEvent{finished: true, err: sc.Err()}:
			case <-stop:
			}
		}(readers[i], stream)
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	poll := time.NewTicker(25 * time.Millisecond)
	defer poll.Stop()
	for {
		select {
		case e := <-events:
			consume(e)
			if !readerOK || exceeded {
				return
			}
		case <-poll.C:
			p, err := native.read(cmd.Process.Pid)
			if err != nil {
				readerOK = false
				return
			}
			if p == nil || !p.live {
				return
			}
		case <-timer.C:
			// A queued reader failure or already-exited nonzero child wins over
			// timer scheduling. Finalization also checks errors before timeout.
			tick()
			p, err := native.read(cmd.Process.Pid)
			if !readerOK || err != nil {
				readerOK = false
				return
			}
			if p == nil || !p.live {
				return
			}
			timedOut = true
			return
		}
	}
}
