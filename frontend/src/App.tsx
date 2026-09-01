import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Navigate, Route, Routes } from "react-router";

import { getHealth } from "./services/api";
import {
  acknowledgeInsecure,
  getToken,
  isInsecureAcknowledged,
  onTokenChange,
} from "./services/auth";
import { AppShell } from "./components/shell/AppShell";
import { SignIn } from "./components/SignIn";
import { AskPage } from "./routes/AskPage";
import { ClusterPage } from "./routes/ClusterPage";
import { ConnectClusterPage } from "./routes/ConnectClusterPage";
import { FleetPage } from "./routes/FleetPage";
import { InvestigatePage } from "./routes/InvestigatePage";
import { InvestigationPage } from "./routes/InvestigationPage";
import { ReportsPage } from "./routes/ReportsPage";
import { SettingsPage } from "./routes/SettingsPage";
function AuthenticatedApp() {
  const { data: health, isError } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    retry: false,
  });

  const [token, setTokenState] = useState(getToken);
  const [authError, setAuthError] = useState("");
  const [acknowledged, setAcknowledged] = useState(isInsecureAcknowledged);

  // `http.ts` clears the credential when the backend answers 401, which fires
  // here. That is what turns an expired token into a sign-in prompt rather
  // than a screen full of failed requests.
  useEffect(
    () =>
      onTokenChange((next) => {
        setTokenState(next);
        setAcknowledged(isInsecureAcknowledged());
        if (!next) {
          setAuthError("Your session is no longer valid. Sign in again to continue.");
        }
      }),
    [],
  );

  const insecure = health?.insecure ?? false;
  const reachable = !isError && health !== undefined;

  // A backend that needs no credential still gets acknowledged once per tab,
  // so a dangerous configuration is visible rather than silent.
  if (reachable && insecure && !acknowledged) {
    return (
      <SignIn
        health={health}
        onAuthenticated={() => {
          acknowledgeInsecure();
          setAcknowledged(true);
        }}
      />
    );
  }

  if (!insecure && !token) {
    return (
      <SignIn
        health={reachable ? health : undefined}
        error={authError}
        onAuthenticated={() => setAuthError("")}
      />
    );
  }

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<FleetPage />} />
        <Route path="/clusters/:context" element={<ClusterPage />} />
        <Route path="/investigations" element={<InvestigatePage />} />
        <Route path="/connect" element={<ConnectClusterPage />} />
        <Route path="/investigations/:id" element={<InvestigationPage />} />
        <Route path="/ask" element={<AskPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        {/* Destinations arrive as later phases give them data. Until then an
            unknown path returns to the one page that exists rather than 404. */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

function App() {
  return <AuthenticatedApp />;
}

export default App;
