import { Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/sonner";
import { SettingsProvider } from "@/lib/settings";
import Home from "./components/Home";
import SeriesDetail from "./components/SeriesDetail";
import Reader from "./components/Reader";
import Settings from "./components/Settings";
import ImportSession from "./components/ImportSession";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, refetchOnWindowFocus: true },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <SettingsProvider>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/series/:id" element={<SeriesDetail />} />
          <Route path="/read/:volumeId" element={<Reader />} />
          <Route path="/import/:sessionId" element={<ImportSession />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
        <Toaster richColors position="bottom-right" />
      </SettingsProvider>
    </QueryClientProvider>
  );
}
