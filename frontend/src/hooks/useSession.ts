import { useQuery } from "@tanstack/react-query";

import { getSession, permits, type SessionInfo } from "../services/api";

/**
 * The signed-in caller and what they may do.
 *
 * One query key, so the pages that gate on a permission and the settings page
 * that displays the role share a single `/me` response rather than each
 * fetching their own.
 *
 * The console gates on **permissions, not role names**. A role gaining a
 * permission then needs no console change, and there is no second copy of the
 * role table here to drift from `app/authz/models.py`.
 */
export function useSession() {
  const session = useQuery<SessionInfo>({
    queryKey: ["session"],
    queryFn: getSession,
    retry: false,
    staleTime: 60_000,
  });

  return {
    ...session,
    /**
     * Whether the caller holds a permission. Permissive while `/me` is in
     * flight — the backend is the real gate, and flickering every action into
     * a disabled state on each page load is worse than briefly offering one
     * that would 403.
     */
    can: (permission: string) => permits(session.data, permission),
  };
}
