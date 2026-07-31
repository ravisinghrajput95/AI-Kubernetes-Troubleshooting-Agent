package collectors

import "encoding/json"

// jsonString escapes a string as a JSON literal, so a log line containing a
// quote or a newline cannot break the payload it travels in.
func jsonString(value string) (string, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return string(encoded), nil
}
