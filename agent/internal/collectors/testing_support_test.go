package collectors

import (
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/runtime/serializer"
)

// Enough of a scheme for `rest.RESTClientFor` to build a client. The agent
// never decodes into typed objects — it reads raw bytes on purpose — so this
// exists only to satisfy the constructor.
var (
	groupVersion = schema.GroupVersion{Group: "", Version: "v1"}
	codecs       = serializer.NewCodecFactory(runtime.NewScheme())
)
