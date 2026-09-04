export default function RelatoriosPage() {
  return (
    <div>
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Relatórios</h1>
        <p>Indicadores de conferência e glosas evitadas.</p>
      </div>

      <div className="panel">
        <div className="panel-heading">
          <h2>Em construção</h2>
          <span className="state state--off">Issue #7</span>
        </div>
        <p className="empty-state">
          Esta tela ainda não existe. Nenhum relatório ou indicador real ou fictício é exibido
          aqui até que a funcionalidade seja implementada (issue #7).
        </p>
      </div>
    </div>
  );
}
