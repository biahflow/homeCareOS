import { AppShell } from "@/components/shell/AppShell";

export default function AutenticadoLayout({ children }: { children: React.ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
