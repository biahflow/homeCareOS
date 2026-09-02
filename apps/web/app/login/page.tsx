"use client";

import { useRouter } from "next/navigation";
import { useId } from "react";

export default function LoginPage() {
  const router = useRouter();
  const emailId = useId();
  const senhaId = useId();

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Não há backend de autenticação ainda (issue #6): não validamos
    // credenciais, não criamos sessão, só navegamos.
    router.push("/documentos");
  }

  return (
    <main className="grid min-h-svh lg:grid-cols-[1.05fr_1fr]">
      <section
        className="relative hidden overflow-hidden px-12 py-14 text-white lg:flex lg:flex-col lg:justify-between"
        style={{
          background: "linear-gradient(150deg, var(--color-brand-900) 0%, var(--color-ink) 100%)",
        }}
      >
        <div
          aria-hidden
          className="pointer-events-none absolute -top-24 -right-24 size-96 rounded-full border-[60px] border-white/6"
        />
        <div
          aria-hidden
          className="pointer-events-none absolute -bottom-32 -left-16 size-80 rounded-full border-[60px] border-white/6"
        />
        <div className="relative z-10 flex items-center gap-3">
          <span className="grid size-9 place-items-center rounded-lg bg-white/10 text-sm font-bold">
            HC
          </span>
          <span className="text-sm font-semibold tracking-[-0.01em]">
            Home<span className="text-brand-200">CareOS</span>
          </span>
        </div>
        <div className="relative z-10 grid max-w-md gap-4">
          <p className="text-[11px] font-bold uppercase tracking-[0.18em] text-brand-200">
            Faturamento
          </p>
          <h1 className="text-3xl font-semibold tracking-[-0.035em]">
            Confira evoluções de prontuário antes de enviar à operadora.
          </h1>
          <p className="text-sm leading-6 text-white/70">
            Upload, conferência e sinalização de pendências num só lugar — reduzindo glosas antes
            do envio.
          </p>
        </div>
      </section>

      <section className="flex items-center justify-center px-6 py-14">
        <div className="w-full max-w-md">
          <div className="mb-8 grid gap-1.5 lg:hidden">
            <span className="text-sm font-semibold tracking-[-0.01em] text-ink">
              Home<span className="text-brand-500">CareOS</span>
            </span>
          </div>

          <div className="page-head">
            <p className="eyebrow">Acesso</p>
            <h1>Entrar</h1>
            <p>Faturamento e conferência de evoluções.</p>
          </div>

          <form className="form-grid" onSubmit={handleSubmit} noValidate>
            <div className="grid gap-1.5">
              <label htmlFor={emailId} className="form-label">
                E-mail
              </label>
              <input
                id={emailId}
                name="email"
                type="email"
                autoComplete="email"
                placeholder="voce@empresa.com"
                className="field"
              />
            </div>

            <div className="grid gap-1.5">
              <label htmlFor={senhaId} className="form-label">
                Senha
              </label>
              <input
                id={senhaId}
                name="senha"
                type="password"
                autoComplete="current-password"
                placeholder="••••••••"
                className="field"
              />
            </div>

            <button type="submit" className="btn btn--primary mt-2">
              Entrar
            </button>

            <p role="status" className="alert--info mt-2">
              A autenticação ainda não está implementada (issue #6). Este formulário não verifica
              credenciais nem cria uma sessão real — enviar apenas navega para a tela de
              documentos.
            </p>
          </form>
        </div>
      </section>
    </main>
  );
}
