//go:build !linux

package hardening

type Status struct {
	NoNewPrivs bool
	Seccomp    bool
	Landlock   bool
	Limits     bool
}

func (s Status) Encode() string {
	return "no_new_privs-unavailable,seccomp-unavailable,landlock-unavailable,rlimits-unavailable"
}

// Apply keeps Darwin builds portable; runtime confinement remains explicitly unverified.
func Apply(_ int, _ []uint16) (Status, error) { return Status{}, nil }
