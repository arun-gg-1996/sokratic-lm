# Edge Case Evaluation Report

## Metrics

| Split | N | Hit@1 | Hit@3 | Hit@5 | MRR |
|---|---:|---:|---:|---:|---:|
| Original 100 | 100 | 0.450 | 0.500 | 0.500 | 0.473 |
| Edge Cases 50 | 50 | 0.040 | 0.080 | 0.080 | 0.060 |
| Combined 150 | 150 | 0.313 | 0.360 | 0.360 | 0.336 |

### Edge Case Categories

| Category | N | Hit@5 | MRR |
|---|---:|---:|---:|
| Rare topics (10) | 10 | 0.100 | 0.050 |
| Cross-chapter (10) | 10 | 0.000 | 0.000 |
| Informal language (10) | 10 | 0.200 | 0.150 |
| Clinical OT (10) | 10 | 0.000 | 0.000 |
| Single occurrence (10) | 10 | 0.100 | 0.100 |

Lowest Hit@5 category: **Cross-chapter (10)**

## Failure Analysis (MRR=0)

| Query | Expected chunk | Top 1 retrieved | Failure reason |
|---|---|---|---|
| What are the fascicles? | 7325dc76-c565-4a55-b4f8-2bbcc5a6dcdb | None | no result |
| What does this passage state about Pronunciation of words and terms? | b5ae5455-ca72-4aa3-bd90-ab3422b6bccb | None | no result |
| What does this passage state about It separates the thoracic and? | f4bbb283-0606-4eea-821e-c24af82c2aa2 | None | no result |
| What does this passage state about center of gravity? | 4392ddb5-580d-4f35-ae93-2d9b2037d65e | None | no result |
| For a patient with movement issues, what does this text say about quadriceps femoris in the anterior compartment of the thigh? | 01900635-62e3-44c8-a343-a93aaf9bd4aa | 560a8671-c4a3-4445-a52c-a5590b193c91 | other |
| In a patient assessment, what does the textbook state about fixed point that the force? | aa263d9b-300f-49c0-83a2-7001a1adb4ed | None | no result |
| In a patient assessment, what does the textbook state about This arrangement is referred to? | 25a1a402-1b60-4254-a55f-76acb26296c2 | None | no result |
| In a patient assessment, what does the textbook state about One example of this is? | 8fbfb38d-fa2f-4eab-8dd9-4c7742013f64 | None | no result |
| In a patient assessment, what does the textbook state about doctor? | dbde0afa-9943-4c61-8a31-f616474a6aa2 | None | no result |
| How does this passage connect multiple concepts around rectus femoris? | 39039816-766f-4f68-9051-8259837d95b3 | None | no result |
| How does this passage connect multiple concepts around This muscle originates from the? | 1b59cc08-edaf-4359-8bfe-e491f2a64a4c | None | no result |
| How does this passage connect multiple concepts around After proper stretching and warm-up? | c791d89b-2bb0-4286-a0c1-4383a6ffdecb | None | no result |
| What does this passage state about This is analogous to the? | c9e07bde-9f51-4949-a59d-c1b5d14cb9c6 | None | no result |
| What is the area? | 8cf7232b-7eba-4768-b6c6-bda90670be27 | 7eae3cb2-fb11-45fc-85eb-9172739cf976 | wrong chapter |
| What does this passage state about It is responsible for smell? | f28dc0a2-8b45-45fc-a297-4f19a1a70f20 | None | no result |
| In a patient assessment, what does the textbook state about posterior horn? | 13654b37-3ccb-42c1-b898-f05ba0b24994 | None | no result |
| In a patient assessment, what does the textbook state about spinal cord itself? | 27da9d81-afd6-4135-b490-95429d5414bb | None | no result |
| In a patient assessment, what does the textbook state about If you zoom in on? | bc9ce03b-2897-44e5-ad7b-a3737c288731 | None | no result |
| In a patient assessment, what does the textbook state about Those are axons of sensory? | d260174c-ff56-47e5-959b-ead5bdcc29a9 | None | no result |
| In a patient assessment, what does the textbook state about nerves? | f110ccd4-442a-499f-8948-5e11b8192f9e | None | no result |
| In a patient assessment, what does the textbook state about brain stem? | 0d468ff7-853e-40bd-9ec9-885dd8d86b34 | None | no result |
| In a patient assessment, what does the textbook state about first two? | bf26267b-d9ad-4ac4-b507-0f1dec3a0499 | None | no result |
| In a patient assessment, what does the textbook state about facial nerve? | db179d06-738f-47fb-b56c-b3ab0a84fe85 | None | no result |
| How does this passage connect multiple concepts around blockage? | 8b1bac97-1498-4be8-aad9-537d9b9ecf6c | None | no result |
| How does this passage connect multiple concepts around More important are the neurological? | 20fb480a-5489-456c-b6e6-614e0fd06228 | None | no result |
| In a patient assessment, what does the textbook state about Note that this correspondence does? | bafbf421-c6ca-4b32-8018-f69fa85390db | None | no result |
| In a patient assessment, what does the textbook state about Adjacent to these two regions? | 9a795cc8-e4ca-4c9b-ad42-a6659d861545 | None | no result |
| In a patient assessment, what does the textbook state about The first two tastes? | 9d949fa8-7d56-4e81-988d-fad63b1c46d5 | None | no result |
| In a patient assessment, what does the textbook state about The interneuron? | ee6ca3cc-10ce-442f-9e42-6f5cf8036223 | None | no result |
| In a patient assessment, what does the textbook state about way that information? | dafa6165-a885-4290-875d-463738b3be37 | None | no result |
| In a patient assessment, what does the textbook state about outermost layer? | 5b25bf4e-9be1-4008-9e3c-f784da1247ef | None | no result |
| How does this passage connect multiple concepts around Overlaying the ciliary body? | d4135cb2-d839-462a-a81e-485b74700d49 | None | no result |
| What are the scapula and? | 192d3a1e-30c7-4389-a93c-14fb3cfe5d03 | be1a28fa-f742-4cbb-8e72-c5cd75a6ccff | wrong chapter |
| What is the foot? | e71cb52e-3bb8-4282-a7dc-9de0b36f9736 | 1e3852bf-c904-420b-99ca-a5969f4d0eb5 | wrong chapter |
| What does this passage state about This is a pivot joint? | d3f6fac9-1a6b-4260-856b-49b98f386bcd | 9571f994-726d-419f-b912-5c9d716afe45 | other |
| What does this passage state about not all of these? | e8d8b6f1-e65f-498d-9819-9a2898001ea0 | None | no result |
| For a patient with movement issues, what does this text say about The collateral ligaments on the? | 4db66755-fa9f-4578-b6d2-2cda0a8030dd | None | no result |
| How does this passage connect multiple concepts around area of epiphyseal cartilage? | 81b0cff2-c6f4-42ce-a1e5-2d97f5ad6bec | None | no result |
| How does this passage connect multiple concepts around bones? | 417bb790-0ec6-4c6e-89f7-7ed3ce348933 | None | no result |
| How does this passage connect multiple concepts around All of these features allow? | cda5a75c-b128-4b5c-8156-ba88e57bf25c | None | no result |
| How does this passage connect multiple concepts around cecum? | b1d5de78-9042-4804-aaac-35e3664e60aa | None | no result |
| How does this passage connect multiple concepts around In this type of transport? | 3f2069fc-b2f6-412c-a120-20b21bf6d5a3 | None | no result |
| How does this passage connect multiple concepts around As GFR increases? | 2490c306-b7b0-406f-8508-c60cc9a15996 | None | no result |
| How does this passage connect multiple concepts around The energy from ATP drives? | 8e86b6bb-76d3-4787-9703-5a3fdce32100 | None | no result |
| How does ATP relate to muscle contraction according to this text? | a0ea35e9-d60a-4749-bd17-071e6d4e05b5 | ef25e469-7690-4b0a-bc9b-f839590087bc | other |
| How does this passage connect nerve-level and movement-level anatomy? | 409da99f-9352-4181-bfdd-ef3a3aba120b | None | no result |
| How does this passage connect multiple concepts around few enzyme-secreting cells are? | 8bc3e385-5115-463f-872f-9b2be08f4f78 | None | no result |
| How does this passage connect multiple concepts around This repeated movement is known? | 859229cb-95b2-456d-9c8d-4775042442a3 | None | no result |
| How does this passage connect multiple concepts around Although atrophy due to disuse? | 48028ad5-3cbc-462e-8f43-abc957155ed6 | None | no result |
| How does this passage connect multiple concepts around First? | 8b6f6c39-1966-40a1-88c0-ec1117db9a46 | None | no result |
| What does this passage say about the axillary nerve? | 5fad614b-6d81-4e1a-a794-c96be8f95025 | None | no result |
| What does this passage say about the coracobrachialis? | 09058c47-4b13-478e-9274-3051aadbfbfa | None | no result |
| What does this passage say about the extensor carpi radialis? | c210cbf1-48cd-4a60-b752-b9b9b2bacc43 | None | no result |
| What does this passage say about the saphenous nerve? | 5fad614b-6d81-4e1a-a794-c96be8f95025 | None | no result |
| What does this passage say about the brachioradialis? | 5a9c59f4-0115-4da0-a73b-b58bd5d13708 | None | no result |
| What does this passage say about the femoral nerve? | 66a3d88e-ca44-47b2-8c9b-f5d53162a68b | None | no result |
| What does this passage say about the iliopsoas? | 4392ddb5-580d-4f35-ae93-2d9b2037d65e | None | no result |
| What does this passage say about the quadratus lumborum? | 9851f0d7-2479-4065-bde9-e260df6ec5a3 | fcb08936-8ea7-46d0-95ae-1c2588a8f46d | other |
| What does this passage say about the subscapularis? | f22a93e1-d03b-448e-a983-ea345ef461c2 | None | no result |
| What happens at the sarcomere level when the deltoid muscle contracts? | 338b70fd-efaf-4182-af4c-dca28fbd19f9 | 6ce371f1-b67b-4e66-94d9-eb3e618ecd2d | multi-hop / cross-chapter mismatch |
| What spinal cord segments give rise to the nerve that controls deltoid? | 5fad614b-6d81-4e1a-a794-c96be8f95025 | None | no result |
| What is the motor pathway from the brain to the biceps brachii muscle? | cb0a2c98-49d1-4eae-b5c2-a304f83362c3 | None | no result |
| What type of joint is the shoulder and which muscles act on it? | ed725762-7de0-40e1-9072-455c85441efb | 3a761be4-1ddf-4c39-8040-98e731f1dadc | wrong chapter |
| How does a neuromuscular junction work? | c0d607cd-07f2-4392-a094-393d49478d62 | bf3a7c5e-4744-4d8a-9bfd-c96bfe163c95 | wrong chapter |
| What blood vessels supply the deltoid muscle? | 77898622-bbe9-429e-9f11-7ff6b958ca93 | None | no result |
| What metabolic process provides ATP for sustained muscle contraction? | eb89d9ab-20c3-484a-8469-5ee97906a373 | d9b43f29-9ad1-4159-8243-db7a12007774 | wrong chapter |
| What is the difference between the somatic and autonomic nervous systems? | d92fc7d2-bc75-4270-a330-d34502c6fb50 | 4dddb611-d48a-4485-bf44-881fb296a2f6 | wrong chapter |
| What reflex protects the knee joint from hyperextension? | 7d813b65-09a9-4330-8c13-a41f1571a9f5 | 4db66755-fa9f-4578-b6d2-2cda0a8030dd | wrong chapter |
| Which nerve controls wrist extension and what happens if it is damaged? | d260174c-ff56-47e5-959b-ead5bdcc29a9 | None | no result |
| that bump on your shoulder where the muscle attaches | 09058c47-4b13-478e-9274-3051aadbfbfa | 026b4bbb-0a61-4e53-bb0a-dee9fcaaa2f4 | vocabulary mismatch (informal -> formal) |
| what makes your arm go numb when you hit your elbow | 5fad614b-6d81-4e1a-a794-c96be8f95025 | None | no result |
| the muscle tear athletes get in their shoulder | 09058c47-4b13-478e-9274-3051aadbfbfa | None | no result |
| the joint that pops when you crack your knuckles | 737884dd-b79b-4228-9434-2b639eb93578 | None | no result |
| muscle under your armpit | 2d9c3206-2ca8-4f02-92c5-707c723bdf6c | None | no result |
| what holds your bones together at a joint | 737884dd-b79b-4228-9434-2b639eb93578 | 417bb790-0ec6-4c6e-89f7-7ed3ce348933 | vocabulary mismatch (informal -> formal) |
| the nerve damage that causes foot drop | 1400e18f-53e0-4128-9892-e9c437c997ca | a1393be1-4ddd-498a-b5d2-843d2d7703c3 | vocabulary mismatch (informal -> formal) |
| why your muscle shakes when you hold something heavy for too long | 95cbb343-f5bf-46e3-8552-20cf614e4a7e | None | no result |
| A patient presents with inability to abduct the arm after anterior shoulder dislocation. Which nerve is most likely damaged? | 5da38379-cdad-4677-84ed-a915e3dabb21 | None | no result |
| An OT patient has weakness in elbow flexion and forearm supination. Which nerve root level is affected? | 2523b456-d95e-46c0-9306-60b3c37e7cf6 | None | no result |
| A patient cannot extend their wrist after a humeral shaft fracture. Which nerve is compressed? | 912a8a3a-d0ad-495f-a47d-0c4b7a95c1bc | 9bc61d35-7ec7-4a77-bfaa-7e41fbf6516e | wrong chapter |
| After carpal tunnel release surgery, which movement should the OT prioritize rehabilitating first? | eccae9f5-ef0f-4355-ac63-170a52700bd0 | None | no result |
| A patient has weakness in finger abduction and adduction. Which nerve is involved? | d73084f7-42ca-4ba9-bcd6-e45694793925 | None | no result |
| An OT patient cannot shrug their shoulder after neck surgery. Which cranial nerve was damaged? | c84fb49e-eb66-4bc6-9c69-dccfbd43fb2d | None | no result |
| A patient has foot drop after knee replacement surgery. Which nerve was injured during the procedure? | f07f570a-2e36-4330-88d6-c2543a62a5e5 | None | no result |
| After a shoulder dislocation, a patient has numbness over the lateral deltoid region. Which nerve is affected? | 29b82bf3-7ec5-47c7-8b1f-74e6b6a2f2cf | None | no result |
| A patient presents with weakness in hip flexion and knee extension. Which nerve is compromised? | 01900635-62e3-44c8-a343-a93aaf9bd4aa | None | no result |
| An OT patient has a claw hand deformity. Which two nerves are most likely damaged? | 5da38379-cdad-4677-84ed-a915e3dabb21 | None | no result |
| What is the olecranon fossa and what is its clinical significance? | 5cf07728-8adc-478e-a954-c8bba16d7d4a | None | no result |
| What is the calcaneal tendon and what is its clinical significance? | 9ed76486-2e40-4a55-b6f0-3dfca162e0e1 | None | no result |
| What is the saphenous nerve and what is its clinical significance? | 5fad614b-6d81-4e1a-a794-c96be8f95025 | None | no result |
| What is the coracobrachialis and what is its clinical significance? | 09058c47-4b13-478e-9274-3051aadbfbfa | None | no result |
| What is the extensor carpi radialis and what is its clinical significance? | c210cbf1-48cd-4a60-b752-b9b9b2bacc43 | None | no result |
| What is the axillary nerve and what is its clinical significance? | 5fad614b-6d81-4e1a-a794-c96be8f95025 | None | no result |
| What is the teres major and what is its clinical significance? | 901c9c45-0727-4728-8abe-5022b651fc5d | None | no result |
| What is the intertubercular groove and what is its clinical significance? | f61bda60-da9c-4266-8450-a68089cf0618 | None | no result |
| What is the sciatic nerve and what is its clinical significance? | 5fad614b-6d81-4e1a-a794-c96be8f95025 | None | no result |
