"use client";

import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactElement, type ReactNode } from "react";

interface LoadingContextValue {
  setLoading: (key: string, active: boolean) => void;
}

const LoadingContext = createContext<LoadingContextValue>({ setLoading: () => undefined });

export function useSetLoading(): (key: string, active: boolean) => void {
  return useContext(LoadingContext).setLoading;
}

export function LoadingProvider({ children }: { children: ReactNode }): ReactElement {
  const [active, setActive] = useState(false);
  const keys = useRef<Set<string>>(new Set());

  const setLoading = useCallback((key: string, loading: boolean) => {
    if (loading) keys.current.add(key);
    else keys.current.delete(key);
    setActive(keys.current.size > 0);
  }, []);

  const value = useMemo(() => ({ setLoading }), [setLoading]);

  return (
    <LoadingContext.Provider value={value}>
      {active && (
        <div style={{ position: "fixed", top: 0, left: 0, right: 0, height: 3, zIndex: 9999, overflow: "hidden", pointerEvents: "none" }}>
          <div style={{
            height: "100%",
            background: "linear-gradient(90deg, #4361ee, #7c3aed)",
            borderRadius: "0 2px 2px 0",
            animation: "top-bar 1.2s ease-in-out infinite",
          }} />
          <style>{`@keyframes top-bar{0%{width:0%;margin-left:0}60%{width:80%;margin-left:0}100%{width:10%;margin-left:100%}}`}</style>
        </div>
      )}
      {children}
    </LoadingContext.Provider>
  );
}
