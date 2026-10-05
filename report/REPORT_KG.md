# Báo cáo benchmark và kiểm tra lỗi GraphRAG

## 1. Kết quả benchmark

Nguồn số liệu: `ket_qua_benchmark_kg.txt`, tạo bởi `python bench_kg.py --judge` trên corpus đầy đủ. Tổng graph có 927 node và 2.370 quan hệ.

### Chi phí indexing

| Hệ thống | Lượt gọi | Input tokens | Output tokens | Chi phí | Thời gian |
|---|---:|---:|---:|---:|---:|
| Flat RAG | 176 | 56.072 | 0 | $0,00112 | 112,4 giây |
| GraphRAG | 196 | 94.858 | 6.402 | $0,01078 | 236,9 giây |

Index graph có chi phí cao hơn khoảng 9,63 lần và mất khoảng 2,11 lần thời gian so với Flat RAG. Phần tăng thêm gồm trích xuất thực thể/quan hệ từ tin và tạo graph.

### Kết quả querying trung bình mỗi câu

| Hệ thống | Recall | Judge | Input tokens | Output tokens | Chi phí/câu | Thời gian/câu |
|---|---:|---:|---:|---:|---:|---:|
| Flat RAG | 0,43 | 1,00 | 694 | 47 | $0,00013 | 2,50 giây |
| GraphRAG | 0,72 | 1,50 | 3.874 | 75 | $0,00062 | 3,73 giây |

GraphRAG tăng recall trung bình 0,29 và judge 0,50 điểm, đổi lại chi phí truy vấn cao hơn khoảng 4,77 lần, độ trễ cao hơn khoảng 1,49 lần và input token nhiều hơn khoảng 5,58 lần. Điểm trung bình không phản ánh hết lỗi từng câu; xem bảng dưới đây.

### Theo từng câu hỏi

| Câu | Flat RAG (recall/judge) | GraphRAG (recall/judge) | Nhận xét |
|---|---:|---:|---|
| Q1 | 1,00 / 2 | 1,00 / 2 | Hòa; cả hai trả lời được định nghĩa tiền chất. |
| Q2 | 1,00 / 2 | 1,00 / 2 | Hòa. |
| Q3 | 0,00 / 0 | 0,67 / 1 | GraphRAG có thông tin vụ án nhưng câu trả lời nêu sai mức án của Lê Minh Thành. |
| Q4 | 0,00 / 0 | 0,67 / 1 | GraphRAG tìm được Điều 255 nhưng chỉ nêu khoản 1, không trả lời mức tối đa theo khoản 4. |
| Q5 | 0,60 / 1 | 1,00 / 2 | GraphRAG trả đúng khoản 4 điểm b Điều 250 cho lượng MDMA nêu trong câu hỏi. |
| Q6 | 0,00 / 1 | 0,00 / 1 | Hòa theo hai phép đo; kết quả graph có các vụ MDMA nhưng câu trả lời chưa liệt kê đúng các vụ cần thiết. |

`Judge` là đánh giá ngữ nghĩa theo thang điểm benchmark; `recall` đếm các mục `must_include`. Hai số đo có thể khác nhau, như Q6.

## 2. Bằng chứng lỗi

### E2 — Thiếu ngữ cảnh luật/mức phạt tối đa

- **Hiện tượng:** Q4 hỏi mức phạt cao nhất đối với hành vi tổ chức sử dụng trái phép chất ma túy. Câu trả lời GraphRAG nguyên văn: “Giang hồ 'Hoàng Nato' bị bắt về hành vi 'tổ chức sử dụng trái phép chất ma túy'. Hành vi này có thể bị phạt tù từ 02 năm đến 07 năm theo Điều 255 Tội tổ chức sử dụng trái phép chất ma túy, khoản 1.” Đây không phải mức tối đa của Điều 255.
- **Bằng chứng:** Trong Neo4j, truy vấn sau trả ra khoản 4 điểm b với `penalty_text` là “phạt tù 20 năm hoặc tù chung thân”, `imprisonment_max_months = 240` và `life_imprisonment = TRUE`; dữ liệu Điều 255 khoản 4 có trong graph.

  ```cypher
  MATCH (o:Offense)-[:CODIFIED_BY]->(a:LegalArticle)-[:HAS_PROVISION]->(v:Provision)-[:HAS_RULE]->(r:PenaltyRule)
  WHERE a.article_no = 255 AND o.canonical_name CONTAINS 'tổ chức sử dụng'
  RETURN o.canonical_name AS offense, a.article_no AS article, v.paragraph_no AS paragraph,
         v.point AS point, r.penalty_text AS penalty,
         r.imprisonment_max_months AS max_months, r.life_imprisonment AS life;
  ```

- **Nguyên nhân:** Ở bước `Neo4jGraph.context()`, luật được lọc để lấy khoản 1 hoặc khoản có ngưỡng khối lượng khớp. Điều 255 khoản 4 không có ngưỡng khối lượng, nên bị loại dù câu hỏi yêu cầu mức “tối đa”.
- **Đề xuất sửa:** Nhận diện câu hỏi về “cao nhất/tối đa” và truy vấn toàn bộ các khoản, sau đó chọn rule có mức phạt nặng nhất; không dùng điều kiện ngưỡng chất làm bộ lọc duy nhất cho câu hỏi loại này. Thêm kiểm thử đảm bảo Q4 trả khoản 4.

### E5 — LLM trả lời lệch dữ kiện của graph (Q3)

- **Hiện tượng:** Câu trả lời GraphRAG cho Q3 nguyên văn: “Lê Minh Thành bị tuyên phạt 24 tháng tù về tội "mua bán trái phép chất ma túy". Tội này được quy định tại Điều 251 của Bộ luật Hình sự, với khung hình phạt cơ bản là từ 02 năm đến 07 năm tù.” Câu trả lời nêu 24 tháng cho Thành, trong khi nguồn tin và cạnh `INVOLVES_PERSON` gắn với người này ghi mức án 36 tháng tù.
- **Bằng chứng:** Truy vấn Neo4j cho người này trả các bản ghi:

  ```cypher
  MATCH (p:Proceeding)-[r:INVOLVES_PERSON]->(x:Person {name:'Lê Minh Thành'})
  RETURN p.stage AS stage, p.status AS status, r.role AS role, r.sentence AS sentence, x.doc_id AS source
  ORDER BY stage;
  ```

  Kết quả có giai đoạn phúc thẩm với trạng thái “hoãn phiên tòa” và `sentence = "36 tháng tù"`; bản ghi sơ thẩm có trạng thái nhóm “tuyên phạt 24 tháng tù cho mỗi bị cáo” nhưng thuộc tính `sentence` của Lê Minh Thành vẫn là “36 tháng tù”. Nguồn `news-100260918080821054` phân biệt mức án 36 tháng của Thành với mức 24 tháng của các đồng bị cáo.

- **Nguyên nhân:** Trích xuất/biểu diễn đặt thông tin chung của nhóm bị cáo ở `Proceeding.status`, trong khi mức án cá nhân nằm trên quan hệ đến `Person`. Khi cả hai cụm số xuất hiện trong ngữ cảnh, câu trả lời LLM đã chọn mức án chung thay vì thuộc tính cá nhân.
- **Đề xuất sửa:** Giữ tách biệt trạng thái chung của phiên xử và bản án từng người; khi câu hỏi nêu tên cá nhân, ưu tiên dữ kiện `sentence` gắn trực tiếp với người đó. Bổ sung kiểm thử Q3 với các đồng bị cáo có mức án khác nhau.

### E4 — Phép đo recall và judge không đồng thuận (Q6)

- **Hiện tượng:** Q6 có GraphRAG `recall = 0,00` nhưng `judge = 1`, nghĩa là recall không ghi nhận mục bắt buộc nào trong khi judge đánh giá câu trả lời có một phần phù hợp.
- **Bằng chứng:** Câu trả lời GraphRAG nguyên văn:

  > Có hai vụ việc trong tin tức có liên quan đến ma túy MDMA:
  >
  > 1. Vụ tổ chức sử dụng ma túy tại Sầm Sơn, trong đó thu giữ 0,686g ma túy MDMA.
  > 2. Vụ bắt quả tang Thành khi đang mang 5 viên nén màu trắng (là ma túy MDMA) để bán.

  `must_include` yêu cầu tên đầy đủ như “Cái Quang Huy”, “Lê Minh Thành” và “Pháp y tâm thần”. Vì vậy kiểm tra chuỗi bắt buộc cho kết quả recall 0 dù câu trả lời có nêu thông tin MDMA liên quan.
- **Nguyên nhân:** Recall dựa trên khớp cụm từ bắt buộc, không xử lý alias, tên rút gọn hoặc tương đương ngữ nghĩa; judge dùng đánh giá ngữ nghĩa nên hai thước đo không cùng một tiêu chuẩn.
- **Đề xuất sửa:** Chuẩn hóa alias/tên trước khi tính recall và báo cáo riêng exact-match recall với semantic judge. Không dùng một trong hai số làm đại diện duy nhất cho chất lượng.

### Bằng chứng bổ sung: truy vấn tổng hợp MDMA chưa tận dụng hết graph

Graph có ít nhất bốn `CaseEvent` nối với `Substance {substance_key:'MDMA'}`, gồm Cái Quang Huy (9,6 kg), Lê Minh Thành (5 viên), Viện Pháp y tâm thần (không có lượng) và vụ Sầm Sơn. Tuy nhiên câu trả lời Q6 không liệt kê đủ các vụ được hỏi. Truy vấn trực tiếp:

```cypher
MATCH (e:CaseEvent)-[r:INVOLVES_SUBSTANCE]->(s:Substance {substance_key:'MDMA'})
RETURN e.name AS case_name, e.doc_id AS doc_id, r.quantity_text AS quantity
ORDER BY e.doc_id;
```

Nguyên nhân có khả năng nằm ở bước context chỉ mở rộng từ seed của các chunk top-k, thay vì tổng hợp trên toàn bộ các vụ có MDMA. Với câu hỏi dạng “những vụ nào”, cần truy vấn graph theo thực thể/chất được hỏi và tổng hợp danh sách, thay vì phụ thuộc hoàn toàn vào các tài liệu được vector search chọn.

## 3. Ảnh kiểm tra Neo4j Browser

- Phân bố node theo label: [kg_count.png](./img/kg_count.png)
- Đường nối tin tức qua cầu nối sang luật: [kg_cross_kb.png](./img/kg_cross_kb.png)
- Vụ án của Trần Thanh Tuấn đi qua hai KB: [kg_my_case.png](./img/kg_my_case.png)
